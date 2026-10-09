"""Explicit safe debugger reads, separate from the compatible bus memory tools."""

from __future__ import annotations

import hashlib
import re
from typing import Annotated

from pydantic import Field, StrictInt, StrictStr

Address = Annotated[StrictInt, Field(ge=0, le=0xFFFFFFFF)]
PeekLength = Annotated[StrictInt, Field(ge=1, le=4096)]
CPU = Annotated[StrictInt, Field(ge=0, le=1)]
HexBytes = Annotated[StrictStr, Field(min_length=2, max_length=8192, pattern=r"^(?:[0-9a-fA-F]{2})+$")]


def _bytes(value: str) -> bytes:
    if type(value) is not str or not re.fullmatch(r"(?:[0-9a-fA-F]{2}){1,4096}", value):
        raise ValueError("hex data must contain 1..4096 uninterrupted hex byte pairs")
    return bytes.fromhex(value)


def _write(emu, address, hex_data, cpu, expected_hex, *, instruction):
    payload = _bytes(hex_data)
    expected = None if expected_hex is None else _bytes(expected_hex)
    if expected is not None and len(expected) != len(payload):
        raise ValueError("expected_hex must have the same byte length as hex_data")
    read = emu.lib.code_peek_block if instruction else emu.lib.peek_block
    before = read(cpu, address, len(payload))
    if expected is not None and before != expected:
        raise ValueError("EXPECTED_BYTES_MISMATCH: memory changed or the address/view is wrong; nothing was written")
    written = emu.lib.poke_block(cpu, address, payload, instruction=instruction)
    after = read(cpu, address, len(payload))
    if after != payload:
        raise RuntimeError("DEBUG_WRITE_VERIFICATION_FAILED: write completed but readback differs; inspect current memory before continuing")
    return {
        "ok": True, "cpu": cpu, "address": address, "bytes_written": written,
        "previous_hex": before.hex(), "hex": after.hex(),
        "previous_sha256": hashlib.sha256(before).hexdigest(), "sha256": hashlib.sha256(after).hexdigest(),
        "frame_number": emu.lib.get_status()[1], "expected_bytes_checked": expected is not None,
        "access": "debug_patch_instruction_backing" if instruction else "debug_poke_data_view",
        "prefetch_refresh_requested": instruction,
        "notes": (["ARM9 instruction backing excludes DTCM overlay", "affected prefetched bytes are refreshed without executing CPUs"]
                  if instruction else ["CPU data backing view; instruction prefetch is deliberately unchanged", "use code_patch for code changes that must affect the next execution"]),
    }


def register(mcp, emu) -> None:
    @mcp.tool()
    def code_peek(address: Address, length: PeekLength = 256, cpu: CPU = 0) -> dict:
        """Read DS instruction backing bytes without MMIO or execution; ARM9 ITCM is visible, DTCM overlay is excluded. Not an I-cache snapshot."""
        data = emu.lib.code_peek_block(cpu, address, length)
        return {"ok": True, "cpu": cpu, "address": address, "length": length,
                "hex": data.hex(), "sha256": hashlib.sha256(data).hexdigest(),
                "access": "debug_peek_instruction_backing", "frame_number": emu.lib.get_status()[1],
                "notes": ["instruction backing, not I-cache or prefetched slots; ARM9 excludes DTCM overlay"]}

    @mcp.tool()
    def memory_poke(address: Address, hex_data: HexBytes, cpu: CPU = 0,
                    expected_hex: HexBytes | None = None) -> dict:
        """Write DS RAM/WRAM/TCM data backing atomically, with optional expected-byte guard and readback. No BIOS/MMIO/GPU/cart writes; use code_patch for code."""
        return _write(emu, address, hex_data, cpu, expected_hex, instruction=False)

    @mcp.tool()
    def code_patch(address: Address, hex_data: HexBytes, cpu: CPU = 0,
                   expected_hex: HexBytes | None = None) -> dict:
        """Patch DS instruction backing and refresh affected prefetched instructions without executing CPUs. Optional expected_hex prevents stale edits. ARM9 DTCM overlay is excluded."""
        return _write(emu, address, hex_data, cpu, expected_hex, instruction=True)

    @mcp.tool()
    def memory_peek(address: Address, length: PeekLength = 256, cpu: CPU = 0) -> dict:
        """Read DS RAM/WRAM/TCM/BIOS through a side-effect-free CPU data view; no MMIO/GPU/cart/DSi, execution or instruction-cache access."""
        data = emu.lib.peek_block(cpu, address, length)
        return {
            "ok": True, "hex": data.hex(), "sha256": hashlib.sha256(data).hexdigest(),
            "access": "debug_peek_data_view", "cpu": cpu, "address": address,
            "length": length, "frame_number": emu.lib.get_status()[1],
            "notes": [
                "CPU data-side debug view; not the instruction cache or prefetched instruction pipeline",
                "unsupported or cross-region ranges fail; this tool never falls back to a side-effectful bus read",
            ],
        }
