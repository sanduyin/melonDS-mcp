"""Guarded, bounded DS cheat research and ordinary battery-save workflows.

No tool here guesses a game address, imports a savestate, enables AR codes,
advances the game, or overwrites an existing file.
SPDX-License-Identifier: GPL-3.0-or-later
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from typing import Annotated, Literal

from pydantic import Field, StrictInt, StrictStr

from .tools_memory import CPU, _write

Address = Annotated[StrictInt, Field(ge=0x02000000, le=0x023FFFFF)]
Count = Annotated[StrictInt, Field(ge=1, le=4096)]
Stride = Annotated[StrictInt, Field(ge=1, le=65536)]
Width = Annotated[StrictInt, Field(ge=1, le=4)]
Value = Annotated[StrictInt, Field(ge=0, le=0xFFFFFFFF)]
Digest = Annotated[StrictStr, Field(pattern=r"^[0-9a-fA-F]{64}$")]
Values = Annotated[list[Value], Field(min_length=1, max_length=32)]
Pattern = Annotated[StrictStr, Field(pattern=r"^(?:[0-9a-fA-F]{2}){1,16}$")]
Span = Annotated[StrictInt, Field(ge=1, le=4*1024*1024)]


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _file_sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _range(address, size):
    if not (0x02000000 <= address < address + size <= 0x02400000):
        raise ValueError('Only canonical DS main RAM 0x02000000..0x023FFFFF is supported')


def _table_span(address, stride, count, width):
    if width not in (1, 2, 4):
        raise ValueError('Width must be 1, 2 or 4 bytes')
    size = stride * (count - 1) + width
    if stride < width or size > 65536:
        raise ValueError('Non-overlapping fields and a table span <= 65536 bytes are required')
    _range(address, size)
    return size


def _read(emu, cpu, address, size):
    return b''.join(emu.lib.peek_block(cpu, address+i, min(4096, size-i))
                    for i in range(0, size, 4096))


def _values(data, stride, count, width):
    return [int.from_bytes(data[i*stride:i*stride+width], 'little') for i in range(count)]


def register(mcp, emu):
    @mcp.tool()
    def save_workspace_prepare(rom_path: str, save_path: str, workspace_dir: str) -> dict:
        """Copy a user-selected .nds and raw .sav into a NEW isolated directory, retaining an original .sav and hash manifest. Does not load the ROM or touch source files. Load returned rom_path next; MCP is not attached to a GUI."""
        rom, save, dest = (Path(p).resolve() for p in (rom_path, save_path, workspace_dir))
        if rom.suffix.lower() != '.nds' or save.suffix.lower() != '.sav':
            raise ValueError('Expected a .nds ROM and an ordinary raw .sav, not a savestate/.dsv')
        if not rom.is_file() or not save.is_file():
            raise ValueError('ROM and save must both exist')
        if not 1 <= save.stat().st_size <= 64*1024*1024:
            raise ValueError('Save size must be 1..64 MiB; format compatibility still requires testing')
        with rom.open('rb') as stream:
            header = stream.read(0x200)
        if len(header) != 0x200:
            raise ValueError('ROM header is truncated')
        original = {'rom_sha256': _file_sha(rom), 'save_sha256': _file_sha(save)}
        dest.mkdir(parents=True, exist_ok=False)
        # A failure leaves an isolated partial workspace for diagnosis, never source edits.
        for source, name in ((rom, 'game.nds'), (save, 'game.sav'), (save, 'original.sav')):
            with source.open('rb') as src, (dest/name).open('xb') as output:
                shutil.copyfileobj(src, output)
        if (_file_sha(dest/'game.nds') != original['rom_sha256'] or
                any(_file_sha(dest/name) != original['save_sha256'] for name in ('game.sav','original.sav')) or
                _file_sha(save) != original['save_sha256'] or _file_sha(rom) != original['rom_sha256']):
            raise RuntimeError('SOURCE_CHANGED_DURING_COPY: do not load the partial workspace')
        result = {'ok': True, **original, 'rom_path': str(dest/'game.nds'),
                  'save_path': str(dest/'game.sav'), 'backup_path': str(dest/'original.sav'),
                  'game_code': header[12:16].decode('ascii', errors='replace'),
                  'rom_revision': header[0x1E], 'save_bytes': save.stat().st_size}
        with (dest/'manifest.json').open('x', encoding='utf-8') as output:
            json.dump(result, output, indent=2)
        return result

    @mcp.tool()
    def backup_export(path: str) -> dict:
        """Export current cartridge battery data to a NEW .sav, with size/SHA256. This does NOT save pending RAM changes: use the game's save menu first, then cold-boot a separate core to verify persistence. Never overwrites an existing file."""
        if not emu.rom_path:
            raise ValueError('Load an isolated ROM first')
        dest = Path(path).resolve()
        if dest.suffix.lower() != '.sav':
            raise ValueError('Output must be an ordinary .sav')
        if dest.exists():
            raise FileExistsError('Output exists; select a new path (no overwrite)')
        if not dest.parent.is_dir():
            raise ValueError('Output parent directory must exist')
        with tempfile.NamedTemporaryFile(prefix='.melonds-export-', suffix='.sav', dir=dest.parent, delete=False) as stream:
            temp = Path(stream.name)
        try:
            if emu.lib.lib.melonds_backup_export(str(temp).encode('utf-8')) != 1:
                raise RuntimeError('Battery export failed; the cartridge may have no save memory')
            size = temp.stat().st_size
            if not 1 <= size <= 64*1024*1024:
                raise RuntimeError('Invalid exported save length')
            digest = _file_sha(temp)
            # Exclusive creation prevents races from overwriting another file.
            with dest.open('xb') as output, temp.open('rb') as source:
                shutil.copyfileobj(source, output)
            if _file_sha(dest) != digest:
                raise RuntimeError('EXPORT_VERIFICATION_FAILED: inspect the new output file')
            return {'ok': True, 'path': str(dest), 'bytes': size, 'sha256': digest,
                    'cold_boot_verified': False}
        finally:
            temp.unlink(missing_ok=True)

    @mcp.tool()
    def memory_scan(address: Address, span: Span, pattern_hex: Pattern,
                    alignment: Width = 1, cpu: CPU = 0) -> dict:
        """Read-only exact-byte scan of bounded DS main RAM. Encode numbers little-endian. Repeat after a controlled in-game value change to narrow candidates; a match is not proof of an editable field. Returns at most 256 aligned addresses."""
        _range(address, span)
        if alignment not in (1, 2, 4):
            raise ValueError('Alignment must be 1, 2 or 4')
        data = _read(emu, cpu, address, span)
        pattern = bytes.fromhex(pattern_hex)
        matches, pos = [], 0
        while (pos := data.find(pattern, pos)) >= 0:
            if (address+pos) % alignment == 0:
                matches.append(address+pos)
                if len(matches) == 257:
                    break
            pos += 1
        return {'ok': True, 'addresses': matches[:256], 'truncated': len(matches) > 256,
                'sha256': _sha(data), 'frame_number': emu.lib.get_status()[1]}

    @mcp.tool()
    def memory_table_read(address: Address, stride: Stride, count: Count,
                          width: Width = 1, cpu: CPU = 0) -> dict:
        """Inspect a strided little-endian flag/value table, preserving gaps. Returns values, counts and SHA256 of the entire span for memory_table_patch. Max span 64 KiB; wrong-region tables may actually contain IDs, stats or pointers."""
        size = _table_span(address, stride, count, width)
        data = _read(emu, cpu, address, size)
        values = _values(data, stride, count, width)
        return {'ok': True, 'address': address, 'span': size, 'stride': stride,
                'count': count, 'width': width, 'cpu': cpu, 'values': values,
                'counts': dict(Counter(values)), 'sha256': _sha(data),
                'frame_number': emu.lib.get_status()[1]}

    @mcp.tool()
    def memory_table_patch(address: Address, stride: Stride, count: Count,
                           value: Value, replace_values: Values, expected_sha256: Digest,
                           width: Width = 1, cpu: CPU = 0) -> dict:
        """Guarded table edit: only replace explicitly listed old values; preserve all other values and inter-field bytes. Requires fresh whole-span SHA256. Readback verified, no frame advance. Chunked writes are NOT globally atomic: on error discard/reload the isolated copy and inspect before retrying. Does not write a .sav."""
        size = _table_span(address, stride, count, width)
        if any(v >= 1 << (8*width) for v in [value, *replace_values]):
            raise ValueError('Value exceeds field width')
        before = _read(emu, cpu, address, size)
        if _sha(before) != expected_sha256.lower():
            raise ValueError('EXPECTED_BYTES_MISMATCH: stale table or wrong address; nothing was written')
        after = bytearray(before)
        changed = []
        for i, old in enumerate(_values(before, stride, count, width)):
            if old in replace_values and old != value:
                after[i*stride:i*stride+width] = value.to_bytes(width, 'little')
                changed.append(i)
        completed = 0
        try:
            for pos in range(0, size, 4096):
                old, new = before[pos:pos+4096], bytes(after[pos:pos+4096])
                if old != new:
                    _write(emu, address+pos, new.hex(), cpu, old.hex(), instruction=False)
                    completed += 1
            if _read(emu, cpu, address, size) != after:
                raise RuntimeError('Whole-table readback differs')
        except (ValueError, RuntimeError, OSError) as exc:
            raise RuntimeError(f'TABLE_WRITE_INCOMPLETE: {completed} chunks verified; failing chunk may also be modified. Reload isolated backup before retrying. {exc}') from exc
        return {'ok': True, 'changed_count': len(changed), 'changed_indices': changed,
                'previous_sha256': _sha(before), 'sha256': _sha(after),
                'frame_number': emu.lib.get_status()[1], 'persisted_to_save': False}

    @mcp.tool()
    def cheat_generate_ar(address: Address, value: Value, width: Width = 4,
                          count: Count = 1, stride: Stride = 4,
                          activation: Literal['select', 'always'] = 'select') -> dict:
        """Generate (but do NOT install/enable) a bounded direct-write DS Action Replay code for an already verified ARM7-bus main-RAM address. Supports 8/16/32-bit writes and strided loops. Select means repeated while held, NOT edge-triggered one-shot. No ROM compatibility or persistence is implied."""
        _table_span(address, stride, count, width)
        if value >= 1 << (8*width) or address % width or (count > 1 and stride % width):
            raise ValueError('Value must fit width; address/stride must be naturally aligned')
        lines = ['94000130 FFFB0000'] if activation == 'select' else []
        if count > 1:
            lines.append(f'C0000000 {count-1:08X}')
        opcode = {1: 0x20000000, 2: 0x10000000, 4: 0}[width]
        lines.append(f'{opcode | address:08X} {value:08X}')
        if count > 1:
            lines.append(f'DC000000 {stride:08X}')
        lines.append('D2000000 00000000')
        return {'ok': True, 'code': '\n'.join(lines), 'enabled': False,
                'runtime_verified': False, 'activation': activation,
                'warning': 'Unconditional field writes do not preserve stronger/owned flags. Prefer guarded memory_table_patch for one-time save edits. AR uses ARM7 bus, not ARM9 TCM data view.'}
