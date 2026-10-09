"""Native savestate rejection and round-trip regression.

This test uses the generated ``synthetic_rom.py`` fixture only.  Its malformed
inputs are limited to the fixed 16-byte melonDS root header, so all rejection
cases must fail while constructing ``Savestate`` and must never enter component
loaders.  The outer process gives native failures a bounded timeout.

Do not treat this as a hostile-file parser audit; it verifies the concrete
failure boundary used by the MCP savestate workflow.

Usage::

    python mcp/tests/native_savestate_errors.py \
        --library build/mcp-direct/melonds_mcp.dll

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TEST_DIR.parent / "python"))

from melonds_mcp.libmelonds import LibMelonDS
from synthetic_rom import COUNTERS, write_rom


ROOT_HEADER_SIZE = 16
SAVESTATE_MAGIC = b"MELN"
SAVESTATE_MAJOR = 14
SCRATCH_ADDRESS = 0x02003000


@dataclass(eq=True)
class CoreSnapshot:
    """State that rejected loads are forbidden to change."""

    status: list[int]
    registers: list[list[int]]
    ram: bytes
    counters: list[int]

    @property
    def frame(self) -> int:
        return self.status[1]


class NativeSavestateErrors(unittest.TestCase):
    library_path: Path

    def setUp(self) -> None:
        self.workspace = tempfile.TemporaryDirectory(prefix="melonds-savestate-errors-")
        self.addCleanup(self.workspace.cleanup)
        root = Path(self.workspace.name)
        self.bridge = LibMelonDS(self.library_path)
        self.api = self.bridge.lib
        self.assertEqual(self.api.melonds_init(), 0)
        self.addCleanup(self.api.melonds_free)
        self.api.melonds_debug_bp_clear(-1)
        self.api.melonds_debug_wp_clear(-1)
        self.api.melonds_debug_trace_stop()
        self.api.melonds_debug_break_ack()

        rom = write_rom(root / "savestate-errors.nds")
        self.assertEqual(self.api.melonds_open(str(rom).encode("utf-8")), 1)
        self.assertEqual(self.api.melonds_cycle(), 0)
        self.assertEqual(self.api.melonds_cycle(), 0)
        self.api.melonds_pause()

        # Save a recognizable valid baseline.
        self.api.melonds_memory_write32(0, SCRATCH_ADDRESS, 0x11223344)
        self.assertEqual(self.api.melonds_debug_write_register(0, 2, 0x13579BDF), 1)
        self.assertEqual(self.api.melonds_debug_write_register(1, 2, 0x2468ACE0), 1)
        self.baseline = self.snapshot()
        self.valid_path = root / "valid.mst"
        self.assertEqual(self.load_or_save(self.valid_path, save=True), 1)
        self.valid = self.valid_path.read_bytes()
        self.assertGreater(len(self.valid), ROOT_HEADER_SIZE)
        self.assertEqual(self.valid[:4], SAVESTATE_MAGIC)
        major, _minor, declared_length = struct.unpack_from("<HHI", self.valid, 4)
        self.assertEqual(major, SAVESTATE_MAJOR)
        self.assertEqual(declared_length, len(self.valid))

        # Move the live core to a distinct paused state.  Invalid loads must
        # preserve this state exactly, rather than partially restoring baseline.
        self.api.melonds_memory_write32(0, SCRATCH_ADDRESS, 0xA1B2C3D4)
        self.assertEqual(self.api.melonds_debug_write_register(0, 2, 0xDEADBEEF), 1)
        self.assertEqual(self.api.melonds_debug_write_register(1, 2, 0xCAFEBABE), 1)
        self.api.melonds_resume()
        before_frame = self.bridge.get_status()[1]
        self.assertEqual(self.api.melonds_cycle(), 0)
        self.assertEqual(self.bridge.get_status()[1], before_frame + 1)
        self.api.melonds_pause()
        self.current = self.snapshot()
        self.assertNotEqual(self.current.frame, self.baseline.frame)
        self.assertNotEqual(self.current.ram, self.baseline.ram)
        self.assertNotEqual(self.current.registers[0][2], self.baseline.registers[0][2])

    def load_or_save(self, path: Path, *, save: bool = False) -> int:
        encoded = str(path).encode("utf-8")
        function = self.api.melonds_savestate_save if save else self.api.melonds_savestate_load
        return function(encoded)

    def snapshot(self) -> CoreSnapshot:
        # Main RAM uses a side-effect-free debugger view.  Complete register
        # records include both CPSRs and architectural cycle counters.
        return CoreSnapshot(
            status=self.bridge.get_status(),
            registers=[self.bridge.get_registers(cpu) for cpu in (0, 1)],
            ram=self.bridge.peek_block(0, SCRATCH_ADDRESS, 16),
            counters=[int.from_bytes(self.bridge.peek_block(cpu, COUNTERS[cpu], 4), "little")
                      for cpu in (0, 1)],
        )

    def assert_rejected_without_mutation(self, name: str, payload: bytes) -> None:
        path = Path(self.workspace.name) / name
        path.write_bytes(payload)
        before = self.snapshot()
        self.assertEqual(before, self.current)
        self.assertEqual(self.load_or_save(path), 0)
        self.assertEqual(
            self.snapshot(), before,
            "a root-header error must be rejected before NDS component loading",
        )

    def test_bad_magic_is_rejected_before_component_loading(self) -> None:
        malformed = b"BAD!" + self.valid[4:]
        self.assertEqual(len(malformed), len(self.valid))
        self.assert_rejected_without_mutation("bad-magic.mst", malformed)

    def test_truncated_root_header_is_rejected_without_mutation(self) -> None:
        # Seven bytes include a complete magic and major, but not the complete
        # minor/declared-length/reserved root header.
        self.assert_rejected_without_mutation("truncated-header.mst", self.valid[:7])

    def test_inconsistent_declared_length_is_rejected_without_mutation(self) -> None:
        malformed = bytearray(self.valid)
        struct.pack_into("<I", malformed, 8, len(malformed) + ROOT_HEADER_SIZE)
        self.assert_rejected_without_mutation("bad-length.mst", bytes(malformed))

    def test_valid_state_roundtrip_restores_registers_ram_and_frame(self) -> None:
        self.assertEqual(self.snapshot(), self.current)
        self.assertEqual(self.load_or_save(self.valid_path), 1)
        restored = self.snapshot()
        self.assertEqual(restored.status, self.baseline.status)
        self.assertEqual(restored.frame, self.baseline.frame)
        self.assertEqual(restored.ram, self.baseline.ram)
        self.assertEqual(restored.counters, self.baseline.counters)
        for cpu in (0, 1):
            # Slots 20/21 are derived code/data-region diagnostics rather than
            # saved architectural state; all other slots must round-trip.
            self.assertEqual(restored.registers[cpu][:20], self.baseline.registers[cpu][:20])
            self.assertEqual(restored.registers[cpu][22:], self.baseline.registers[cpu][22:])

    def test_previous_mcpr_version_remains_loadable(self) -> None:
        # MCPR is the final optional section. Version 2 adds an explicit CurCPU
        # word; old version-1 snapshots must still load using phase inference.
        offset = ROOT_HEADER_SIZE
        while self.valid[offset:offset + 4] != b"MCPR":
            length = struct.unpack_from("<I", self.valid, offset + 4)[0]
            self.assertGreaterEqual(length, 16)
            offset += length
            self.assertLess(offset, len(self.valid))
        section_length = struct.unpack_from("<I", self.valid, offset + 4)[0]
        self.assertEqual(offset + section_length, len(self.valid))
        self.assertEqual(struct.unpack_from("<I", self.valid, offset + 16)[0], 2)
        legacy = bytearray(self.valid[:-4])
        struct.pack_into("<I", legacy, 8, len(legacy))
        struct.pack_into("<I", legacy, offset + 4, section_length - 4)
        struct.pack_into("<I", legacy, offset + 16, 1)
        path = Path(self.workspace.name) / "legacy-mcpr1.mst"
        path.write_bytes(legacy)
        self.assertEqual(self.load_or_save(path), 1)
        self.assertEqual(self.bridge.get_status()[1], self.baseline.frame)
        self.assertEqual(self.bridge.peek_block(0, SCRATCH_ADDRESS, 16), self.baseline.ram)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--test", action="append", default=[],
                        help="Run one named test method (repeatable).")
    parser.add_argument("--timeout", type=float, default=60,
                        help="Whole-child time limit in seconds (default: 60).")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    args.library = args.library.resolve(strict=True)
    if not args.worker:
        command = [sys.executable, "-u", str(Path(__file__).resolve()),
                   "--library", str(args.library), "--worker"]
        for name in args.test:
            command.extend(["--test", name])
        try:
            return subprocess.run(command, timeout=args.timeout, check=False).returncode
        except subprocess.TimeoutExpired:
            print(f"FAIL: savestate child exceeded {args.timeout:g} seconds", file=sys.stderr)
            return 124

    NativeSavestateErrors.library_path = args.library
    names = args.test or unittest.defaultTestLoader.getTestCaseNames(NativeSavestateErrors)
    suite = unittest.TestSuite(NativeSavestateErrors(name) for name in names)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
