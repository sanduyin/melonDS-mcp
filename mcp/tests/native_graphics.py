"""Read real GPU banks through the native DLL without advancing emulation.

Usage: python mcp/tests/native_graphics.py --library build/mcp-direct/melonds_mcp.dll
Uses only the generated homebrew fixture and Python's standard library.
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import ctypes
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TEST_DIR.parent / "python"))

from melonds_mcp.libmelonds import LibMelonDS
from synthetic_rom import write_rom


# The physical bank layout and LCDC write addresses come from GPU.h's
# VRAM/VRAMMask and WriteVRAM_LCDC tables. H/I's control bytes skip WRAMCNT.
VRAM_SIZES = (0x20000, 0x20000, 0x20000, 0x20000, 0x10000,
              0x4000, 0x4000, 0x8000, 0x4000)
VRAM_LCDC = (0x06800000, 0x06820000, 0x06840000, 0x06860000,
             0x06880000, 0x06890000, 0x06894000, 0x06898000, 0x068A0000)
VRAM_CONTROL = (0x04000240, 0x04000241, 0x04000242, 0x04000243,
                0x04000244, 0x04000245, 0x04000246, 0x04000248, 0x04000249)


class NativeGraphics(unittest.TestCase):
    library_path: Path

    def setUp(self) -> None:
        self.workspace = tempfile.TemporaryDirectory(prefix="melonds-gpu-")
        self.addCleanup(self.workspace.cleanup)
        self.bridge = LibMelonDS(self.library_path)
        self.api = self.bridge.lib
        self.api.melonds_gpu_read.argtypes = [ctypes.c_int, ctypes.c_int,
                                             ctypes.c_uint32,
                                             ctypes.POINTER(ctypes.c_uint8),
                                             ctypes.c_uint32]
        self.api.melonds_gpu_read.restype = ctypes.c_uint32
        self.api.melonds_gpu_state.argtypes = [ctypes.POINTER(ctypes.c_uint32),
                                              ctypes.c_uint32]
        self.api.melonds_gpu_state.restype = ctypes.c_int
        self.assertEqual(self.api.melonds_init(), 0)
        self.addCleanup(self.api.melonds_free)
        self.api.melonds_debug_bp_clear(-1)
        self.api.melonds_debug_wp_clear(-1)
        self.api.melonds_debug_trace_stop()
        self.api.melonds_debug_break_ack()
        rom = write_rom(Path(self.workspace.name) / "gpu.nds")
        self.assertEqual(self.api.melonds_open(str(rom).encode("utf-8")), 1)
        self.api.melonds_pause()
        # Palette and OAM CPU writes are power-gated for each 2D engine.
        self.api.melonds_memory_write16(0, 0x04000304, 0x820F)

    def read(self, region: int, bank: int, offset: int, length: int) -> bytes:
        output = (ctypes.c_uint8 * length)()
        self.assertEqual(self.api.melonds_gpu_read(region, bank, offset, output, length),
                         length)
        return bytes(output)

    def state(self) -> list[int]:
        words = (ctypes.c_uint32 * 16)()
        self.assertEqual(self.api.melonds_gpu_state(words, 16), 16)
        return list(words)

    def write_words(self, address: int, values: list[int]) -> bytes:
        # The core deliberately ignores byte writes to palette/VRAM/OAM.
        for index, value in enumerate(values):
            self.api.melonds_memory_write16(0, address + 2 * index, value)
        return struct.pack(f"<{len(values)}H", *values)

    def test_all_nine_physical_vram_banks_and_unmapped_reads(self) -> None:
        expected = []
        for bank, (base, size, control) in enumerate(
            zip(VRAM_LCDC, VRAM_SIZES, VRAM_CONTROL, strict=True)
        ):
            with self.subTest(bank=bank):
                self.api.melonds_memory_write8(0, control, 0x80)  # LCDC enabled
                first = self.write_words(base, [0x1200 + bank * 0x10 + i for i in range(16)])
                last = self.write_words(base + size - 8, [0xA000 + bank * 0x10 + i for i in range(4)])
                data = self.read(0, bank, 0, size)
                self.assertEqual(data[:len(first)], first)
                self.assertEqual(data[-len(last):], last)
                self.assertEqual(self.read(0, bank, 1, 7), first[1:8])
                self.assertEqual(self.read(0, bank, size - 1, 1), last[-1:])
                expected.append((first, last))
                self.api.melonds_memory_write8(0, control, 0)  # Unmap; retain storage
                self.assertEqual(self.api.melonds_memory_read16(0, base), 0)

        # A physical inspection must still see the distinct stored bytes when
        # all banks are unmapped from CPU address space.
        for bank, (first, last) in enumerate(expected):
            self.assertEqual(self.read(0, bank, 0, len(first)), first)
            self.assertEqual(self.read(0, bank, VRAM_SIZES[bank] - len(last), len(last)), last)

    def test_standard_palettes_include_each_engines_bg_and_obj_halves(self) -> None:
        for engine in (0, 1):
            with self.subTest(engine=engine):
                base = 0x05000000 + engine * 0x400
                pieces = {}
                for offset in (0, 0x200, 0x3F8):
                    words = [0x4000 + engine * 0x1000 + offset + i for i in range(4)]
                    pieces[offset] = self.write_words(base + offset, words)
                data = self.read(1, engine, 0, 0x400)
                for offset, payload in pieces.items():
                    self.assertEqual(data[offset:offset + len(payload)], payload)
                self.assertEqual(self.read(1, engine, 0x201, 5), pieces[0x200][1:6])

    def test_both_oam_banks_include_attributes_and_affine_parameter_words(self) -> None:
        for engine in (0, 1):
            with self.subTest(engine=engine):
                base = 0x07000000 + engine * 0x400
                first = self.write_words(base, [0x1000 + engine, 0x2345, 0x3456, 0x0100,
                                                0x5678, 0x6789, 0x789A, 0xFF80])
                last = self.write_words(base + 0x3F8, [0x1111, 0x2222, 0x3333, 0x4444 + engine])
                data = self.read(2, engine, 0, 0x400)
                self.assertEqual(data[:len(first)], first)
                self.assertEqual(data[-len(last):], last)
                self.assertEqual(self.read(2, engine, 6, 2), b"\x00\x01")

    def test_gpu_state_has_exact_16_word_layout(self) -> None:
        display_a, display_b = 0x04010301, 0x00010402
        self.api.melonds_memory_write32(0, 0x04000000, display_a)
        self.api.melonds_memory_write32(0, 0x04001000, display_b)
        controls = [0x80, 0x81, 0x82, 0x83, 0x84, 0x85, 0x80, 0x81, 0x82]
        for address, value in zip(VRAM_CONTROL, controls, strict=True):
            self.api.melonds_memory_write8(0, address, value)
        words = (ctypes.c_uint32 * 18)(*[0xDEADBEEF] * 18)
        self.assertEqual(self.api.melonds_gpu_state(words, 18), 16)
        expected = [self.bridge.get_status()[1],
                    self.api.melonds_memory_read16(0, 0x04000006),
                    display_a, display_b, 0x820F, *controls, 0, 0]
        self.assertEqual(list(words[:16]), expected)
        self.assertEqual(list(words[16:]), [0xDEADBEEF, 0xDEADBEEF])

    def test_invalid_ranges_and_capacities_leave_output_untouched(self) -> None:
        failures = [(-1, 0, 0, 1), (3, 0, 0, 1), (0, -1, 0, 1), (0, 9, 0, 1),
                    (1, -1, 0, 1), (1, 2, 0, 1), (2, -1, 0, 1), (2, 2, 0, 1),
                    (0, 0, 0xFFFFFFFF, 1), (0, 0, 1, 0xFFFFFFFF), (0, 0, 0, 0)]
        for bank, size in enumerate(VRAM_SIZES):
            failures.extend([(0, bank, size - 1, 2), (0, bank, size, 1)])
        for region in (1, 2):
            for bank in (0, 1):
                failures.extend([(region, bank, 0x3FF, 2), (region, bank, 0x400, 1)])
        for request in failures:
            with self.subTest(request=request):
                output = (ctypes.c_uint8 * 32)(*[0xA5] * 32)
                region, bank, offset, length = request
                self.assertEqual(self.api.melonds_gpu_read(region, bank, offset, output, length), 0)
                self.assertEqual(bytes(output), b"\xA5" * 32)
        self.assertEqual(self.api.melonds_gpu_read(0, 0, 0, None, 1), 0)
        self.assertEqual(self.api.melonds_gpu_state(None, 16), 0)
        for capacity in (0, 1, 15):
            words = (ctypes.c_uint32 * 16)(*[0xDEADBEEF] * 16)
            self.assertEqual(self.api.melonds_gpu_state(words, capacity), 0)
            self.assertEqual(list(words), [0xDEADBEEF] * 16)

    def test_reads_do_not_advance_or_mutate_stopped_emulator(self) -> None:
        # Reach an actual nonzero scanline, then freeze both cores mid-frame.
        self.api.melonds_resume()
        self.api.melonds_debug_step_request(0, 10000)
        self.assertEqual(self.api.melonds_cycle(), 1)
        self.api.melonds_pause()
        gpu_state = self.state()
        self.assertGreater(gpu_state[1], 0)
        status = self.bridge.get_status()
        registers = [self.bridge.get_registers(cpu) for cpu in (0, 1)]
        clocks = [self.api.melonds_get_cycles(mode) for mode in (0, 2)]
        break_info = self.bridge.break_info()
        for _ in range(3):
            for bank, size in enumerate(VRAM_SIZES):
                self.read(0, bank, 0, size)
            for region in (1, 2):
                for bank in (0, 1):
                    self.read(region, bank, 0, 0x400)
            self.assertEqual(self.state(), gpu_state)
            self.assertEqual(self.bridge.get_status(), status)
            self.assertEqual([self.bridge.get_registers(cpu) for cpu in (0, 1)], registers)
            self.assertEqual([self.api.melonds_get_cycles(mode) for mode in (0, 2)], clocks)
            self.assertEqual(self.bridge.break_info(), break_info)

    def test_uninitialized_state_rejects_without_writing(self) -> None:
        self.api.melonds_free()
        output = (ctypes.c_uint8 * 8)(*[0xA5] * 8)
        words = (ctypes.c_uint32 * 16)(*[0xDEADBEEF] * 16)
        self.assertEqual(self.api.melonds_gpu_read(0, 0, 0, output, 8), 0)
        self.assertEqual(self.api.melonds_gpu_state(words, 16), 0)
        self.assertEqual(bytes(output), b"\xA5" * 8)
        self.assertEqual(list(words), [0xDEADBEEF] * 16)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--test", action="append", default=[])
    parser.add_argument("--timeout", type=float, default=60)
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
            print(f"FAIL: native graphics child exceeded {args.timeout:g} seconds", file=sys.stderr)
            return 124
    NativeGraphics.library_path = args.library
    names = args.test or unittest.defaultTestLoader.getTestCaseNames(NativeGraphics)
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.TestSuite(NativeGraphics(name) for name in names)
    )
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
