"""Real DS memory-peek regressions, including CPU-written ITCM and DTCM.

Usage: python mcp/tests/native_memory_peek.py --library build/mcp-direct/melonds_mcp.dll
The tiny ARM programs are original test code; no ROM/BIOS download is needed.
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
from synthetic_rom import CODE_BASES, write_rom

STUB_ADDRESS = 0x02000800
WRAM_CONTROL = 0x04000247


class NativeMemoryPeek(unittest.TestCase):
    library_path: Path

    def setUp(self) -> None:
        self.workspace = tempfile.TemporaryDirectory(prefix="melonds-peek-")
        self.addCleanup(self.workspace.cleanup)
        self.bridge = LibMelonDS(self.library_path)
        self.api = self.bridge.lib
        self.api.melonds_memory_peek_block.argtypes = [ctypes.c_int, ctypes.c_uint32,
                                                      ctypes.c_uint32,
                                                      ctypes.POINTER(ctypes.c_uint8)]
        self.api.melonds_memory_peek_block.restype = ctypes.c_uint32
        self.assertEqual(self.api.melonds_init(), 0)
        self.addCleanup(self.api.melonds_free)
        self.api.melonds_debug_bp_clear(-1)
        self.api.melonds_debug_wp_clear(-1)
        self.api.melonds_debug_trace_stop()
        self.api.melonds_debug_break_ack()
        rom = write_rom(Path(self.workspace.name) / "peek.nds")
        self.assertEqual(self.api.melonds_open(str(rom).encode("utf-8")), 1)
        self.api.melonds_pause()

    def peek(self, cpu: int, address: int, length: int) -> bytes:
        output = (ctypes.c_uint8 * length)()
        self.assertEqual(self.api.melonds_memory_peek_block(cpu, address, length, output), length)
        return bytes(output)

    def rejected(self, cpu: int, address: int, length: int) -> None:
        output = (ctypes.c_uint8 * 8192)(*[0xA5] * 8192)
        self.assertEqual(self.api.melonds_memory_peek_block(cpu, address, length, output), 0)
        self.assertEqual(bytes(output), b"\xA5" * 8192,
                         "a rejected range must not expose partial output")

    def run_arm9(self, instructions: list[int], registers: dict[int, int]) -> None:
        # Install original ARM test instructions in ordinary RAM, then let the
        # real interpreter perform all CP15/TCM accesses. The old bus-write API
        # cannot populate TCM, so this catches a peek accidentally using it too.
        for index, instruction in enumerate([*instructions, 0xEAFFFFFE]):
            self.api.melonds_memory_write32(0, STUB_ADDRESS + index * 4, instruction)
        for index, value in registers.items():
            self.assertEqual(self.api.melonds_debug_write_register(0, index, value), 1)
        self.assertEqual(self.api.melonds_debug_write_register(0, 15, STUB_ADDRESS), 1)
        self.api.melonds_debug_step_request(0, len(instructions))
        self.api.melonds_resume()
        self.assertEqual(self.api.melonds_cycle(), 1)
        hit = self.bridge.break_info()
        self.assertEqual((hit["cpu"], hit["reason"], hit["pc"]),
                         (0, 3, STUB_ADDRESS + len(instructions) * 4))
        self.api.melonds_pause()

    def cpu_write_and_read(self, address: int, value: int) -> None:
        # str r1,[r0]; ldr r3,[r0]
        self.run_arm9([0xE5801000, 0xE5903000], {0: address, 1: value, 3: 0})
        self.assertEqual(self.bridge.get_registers(0)[3], value,
                         "the real ARM9 must first observe its own TCM write")

    def test_cpu_written_itcm_and_its_virtual_mirrors(self) -> None:
        # NDS::SetupDirectBoot configures a 32 MiB ITCM virtual window backed
        # by 32 KiB physical storage; virtual size is not physical capacity.
        value = 0x1234ABCD
        self.cpu_write_and_read(0x01000400, value)
        expected = struct.pack("<I", value)
        self.assertEqual(self.peek(0, 0x01000400, 4), expected)
        self.assertEqual(self.peek(0, 0x00000400, 4), expected)
        self.assertEqual(self.peek(0, 0x00008400, 4), expected)
        self.assertEqual(self.peek(0, 0x01000401, 2), expected[1:3])
        self.rejected(1, 0x01000400, 4)  # ARM7 has no ITCM overlay.

    def test_cpu_written_dtcm_overrides_shared_wram_data_view(self) -> None:
        self.api.melonds_memory_write8(0, WRAM_CONTROL, 0)
        self.api.melonds_memory_write32(0, 0x03000020, 0x0BADCAFE)  # Physical shared WRAM.
        self.cpu_write_and_read(0x03000020, 0x89ABCDEF)
        self.assertEqual(self.peek(0, 0x03000020, 4), struct.pack("<I", 0x89ABCDEF))
        self.assertEqual(self.api.melonds_memory_read32(0, 0x03000020), 0x0BADCAFE,
                         "legacy bus view must remain distinct and compatible")

    def test_dtcm_relocation_and_tcm_enable_bits_are_observed(self) -> None:
        # mcr p15,0,r0,c9,c1,0 -- DTCM base/size. Map it over a main-RAM mirror.
        address = 0x027C0020
        self.api.melonds_memory_write32(0, address, 0x76543210)
        self.run_arm9([0xEE090F11], {0: 0x027C000A})
        self.cpu_write_and_read(address, 0x13579BDF)
        self.assertEqual(self.peek(0, address, 4), struct.pack("<I", 0x13579BDF))
        self.assertEqual(self.peek(1, address, 4), struct.pack("<I", 0x76543210))

        # mcr p15,0,r0,c1,c0,0 -- remove direct-boot DTCM/ITCM enable bits.
        self.run_arm9([0xEE010F10], {0: 0x00002078})
        self.assertEqual(self.peek(0, address, 4), struct.pack("<I", 0x76543210))
        self.rejected(0, 0x00000400, 4)

    def test_main_ram_mirrors_and_maximum_length(self) -> None:
        payload = bytes((index * 29 + 7) & 0xFF for index in range(4096))
        self.assertEqual(self.bridge.write_block(0, 0x02006000, payload), 4096)
        for cpu in (0, 1):
            for address in (0x02006000, 0x02406000, 0x02806000, 0x02C06000):
                with self.subTest(cpu=cpu, address=hex(address)):
                    self.assertEqual(self.peek(cpu, address, 4096), payload)

    def test_shared_wram_banks_and_arm7_private_fallback(self) -> None:
        low, high, private = 0x11223344, 0x55667788, 0x99AABBCC
        self.api.melonds_memory_write8(0, WRAM_CONTROL, 3)  # All shared RAM -> ARM7.
        self.api.melonds_memory_write32(1, 0x03000080, low)
        self.api.melonds_memory_write32(1, 0x03800100, private)
        self.assertEqual(self.peek(1, 0x03000080, 4), struct.pack("<I", low))
        self.assertEqual(self.peek(1, 0x03810100, 4), struct.pack("<I", private))
        self.rejected(0, 0x03004080, 4)  # Outside DTCM and ARM9 has no shared bank.

        self.api.melonds_memory_write8(0, WRAM_CONTROL, 0)  # All shared RAM -> ARM9.
        self.api.melonds_memory_write32(0, 0x03004080, high)
        self.assertEqual(self.peek(0, 0x03004080, 4), struct.pack("<I", high))
        self.assertEqual(self.peek(1, 0x03000100, 4), struct.pack("<I", private))

        self.api.melonds_memory_write8(0, WRAM_CONTROL, 1)
        self.assertEqual(self.peek(0, 0x03004080, 4), struct.pack("<I", high))
        self.assertEqual(self.peek(1, 0x03000080, 4), struct.pack("<I", low))
        self.api.melonds_memory_write8(0, WRAM_CONTROL, 2)
        self.assertEqual(self.peek(0, 0x03004080, 4), struct.pack("<I", low))
        self.assertEqual(self.peek(1, 0x03000080, 4), struct.pack("<I", high))

    def test_bios_is_debugger_backing_view_not_arm7_pc_protection(self) -> None:
        self.assertEqual(self.peek(0, 0xFFFF0000, 32), self.bridge.read_block(0, 0xFFFF0000, 32))
        image = self.peek(1, 0, 32)
        self.assertNotEqual(image, b"\xFF" * 32)
        self.assertEqual(self.bridge.read_block(1, 0, 32), b"\xFF" * 32)
        self.assertEqual(self.api.melonds_debug_write_register(1, 15, 8), 1)
        self.assertEqual(self.bridge.read_block(1, 0, 32), image)
        self.assertEqual(self.api.melonds_debug_write_register(1, 15, CODE_BASES[1]), 1)
        self.assertEqual(self.peek(1, 0, 32), image)

    def test_rejected_ranges_never_write_a_valid_prefix(self) -> None:
        self.api.melonds_memory_write8(0, WRAM_CONTROL, 0)
        requests = [(-1, 0x02000000, 1), (2, 0x02000000, 1),
                    (0, 0x02000000, 0), (0, 0x02000000, 4097),
                    (0, 0xFFFFFFF0, 32), (1, 0xFFFFFFFF, 2),
                    (0, 0x03FFFFFE, 4), (0, 0xFFFF0FFE, 4), (1, 0x3FFF, 2)]
        for cpu in (0, 1):
            for address in (0x04000000, 0x04100000, 0x04100010, 0x04800000,
                            0x05000000, 0x06000000, 0x06800000, 0x07000000,
                            0x08000000, 0x0A000000):
                requests.append((cpu, address, 4))
        for request in requests:
            with self.subTest(request=request):
                self.rejected(*request)
        self.assertEqual(self.api.melonds_memory_peek_block(0, 0x02000000, 4, None), 0)

    def test_peek_changes_no_cpu_clock_watch_or_break_state(self) -> None:
        self.cpu_write_and_read(0x03000020, 0x12345678)
        wp = self.api.melonds_debug_wp_add(0, 0x03000020, 4, 1)
        self.assertGreater(wp, 0)
        before_status = self.bridge.get_status()
        before_registers = [self.bridge.get_registers(cpu) for cpu in (0, 1)]
        before_clocks = [self.api.melonds_get_cycles(mode) for mode in (0, 2)]
        before_break = self.bridge.break_info()
        before_vcount = self.api.melonds_memory_read16(0, 0x04000006)
        self.assertEqual(self.bridge.wp_events(), [])
        for _ in range(4):
            self.peek(0, 0x03000020, 4)
            self.peek(0, 0x00000400, 32)
            self.peek(1, 0x03800000, 4096)
            self.peek(1, 0, 32)
            self.rejected(0, 0x04100000, 4)
        self.assertEqual(self.bridge.get_status(), before_status)
        self.assertEqual([self.bridge.get_registers(cpu) for cpu in (0, 1)], before_registers)
        self.assertEqual([self.api.melonds_get_cycles(mode) for mode in (0, 2)], before_clocks)
        self.assertEqual(self.bridge.break_info(), before_break)
        self.assertEqual(self.api.melonds_memory_read16(0, 0x04000006), before_vcount)
        self.assertEqual(self.bridge.wp_events(), [])

    def test_uninitialized_emulator_is_rejected(self) -> None:
        self.api.melonds_free()
        self.rejected(0, 0x02000000, 4)


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
            print(f"FAIL: native peek child exceeded {args.timeout:g} seconds", file=sys.stderr)
            return 124
    NativeMemoryPeek.library_path = args.library
    names = args.test or unittest.defaultTestLoader.getTestCaseNames(NativeMemoryPeek)
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.TestSuite(NativeMemoryPeek(name) for name in names)
    )
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
