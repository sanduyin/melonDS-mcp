"""Real interpreter code-patch and TCM data-poke regressions.

Usage: python mcp/tests/native_memory_poke.py --library build/mcp-direct/melonds_mcp.dll
The original ARM/Thumb snippets need no downloaded ROM or BIOS. These tests
exercise interpreter execution, not a claim of JIT runtime coverage.
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

WRAM_CONTROL = 0x04000247
STUB_ADDRESS = 0x02000800


def arm(*words: int) -> bytes:
    return struct.pack("<" + "I" * len(words), *words)


def thumb(*halfwords: int) -> bytes:
    return struct.pack("<" + "H" * len(halfwords), *halfwords)


class NativeMemoryPoke(unittest.TestCase):
    library_path: Path

    def setUp(self) -> None:
        self.workspace = tempfile.TemporaryDirectory(prefix="melonds-poke-")
        self.addCleanup(self.workspace.cleanup)
        self.bridge = LibMelonDS(self.library_path)
        self.api = self.bridge.lib
        byte_pointer = ctypes.POINTER(ctypes.c_uint8)
        for name in ("melonds_memory_peek_block", "melonds_code_peek_block"):
            function = getattr(self.api, name)
            function.argtypes = [ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32, byte_pointer]
            function.restype = ctypes.c_uint32
        self.api.melonds_memory_poke_block.argtypes = [
            ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32, byte_pointer, ctypes.c_int]
        self.api.melonds_memory_poke_block.restype = ctypes.c_uint32
        self.assertEqual(self.api.melonds_init(), 0)
        self.addCleanup(self.api.melonds_free)
        self.api.melonds_debug_bp_clear(-1)
        self.api.melonds_debug_wp_clear(-1)
        self.api.melonds_debug_trace_stop()
        self.api.melonds_debug_break_ack()
        rom = write_rom(Path(self.workspace.name) / "poke.nds")
        self.assertEqual(self.api.melonds_open(str(rom).encode("utf-8")), 1)
        self.api.melonds_pause()

    def peek(self, cpu: int, address: int, length: int, *, code: bool = False) -> bytes:
        output = (ctypes.c_uint8 * length)()
        function = self.api.melonds_code_peek_block if code else self.api.melonds_memory_peek_block
        self.assertEqual(function(cpu, address, length, output), length)
        return bytes(output)

    def poke(self, cpu: int, address: int, data: bytes, *, view: int = 0) -> None:
        source = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
        self.assertEqual(self.api.melonds_memory_poke_block(cpu, address, len(data), source, view),
                         len(data))

    def snapshot(self) -> tuple:
        # Cycle mode 1 intentionally changes its baseline; modes 0 and 2 do not.
        return (self.bridge.get_status(),
                [self.bridge.get_registers(cpu) for cpu in (0, 1)],
                [self.api.melonds_get_cycles(mode) for mode in (0, 2)],
                self.bridge.break_info(),
                self.api.melonds_memory_read16(0, 0x04000006))

    def step(self, cpu: int, count: int) -> None:
        self.api.melonds_debug_step_request(cpu, count)
        self.api.melonds_resume()
        self.assertEqual(self.api.melonds_cycle(), 1)
        self.assertEqual((self.bridge.break_info()["cpu"], self.bridge.break_info()["reason"]),
                         (cpu, 3))
        self.api.melonds_pause()

    def set_program(self, cpu: int, address: int, data: bytes, *, is_thumb: bool = False) -> None:
        self.poke(cpu, address, data, view=1)
        for register in (2, 3, 4):
            self.assertEqual(self.api.melonds_debug_write_register(cpu, register, 0), 1)
        current_thumb = bool(self.bridge.get_registers(cpu)[16] & 0x20)
        if current_thumb == is_thumb:
            self.assertEqual(self.api.melonds_debug_write_register(cpu, 15,
                                                                 address | int(is_thumb)), 1)
        else:
            # The debugger intentionally preserves CPSR.T on PC writes. Use a
            # real BX r5 to switch instruction sets and populate the pipeline.
            trampoline = 0x02000C00 + cpu * 0x20
            self.poke(cpu, trampoline,
                      thumb(0x4728, 0xE7FE) if current_thumb else arm(0xE12FFF15, 0xEAFFFFFE),
                      view=1)
            self.assertEqual(self.api.melonds_debug_write_register(cpu, 5,
                                                                 address | int(is_thumb)), 1)
            self.assertEqual(self.api.melonds_debug_write_register(cpu, 15,
                                                                 trampoline | int(current_thumb)), 1)
            self.step(cpu, 1)
            self.assertEqual(bool(self.bridge.get_registers(cpu)[16] & 0x20), is_thumb)

    def run_arm9(self, instructions: list[int], registers: dict[int, int]) -> None:
        self.set_program(0, STUB_ADDRESS, arm(*instructions, 0xEAFFFFFE))
        for register, value in registers.items():
            self.assertEqual(self.api.melonds_debug_write_register(0, register, value), 1)
        self.step(0, len(instructions))

    def test_arm_current_and_next_prefetch_on_both_cpus(self) -> None:
        for cpu in (0, 1):
            with self.subTest(cpu=cpu):
                address = 0x02008000 + cpu * 0x100
                self.set_program(cpu, address, arm(0xE3A02011, 0xE3A03022, 0xEAFFFFFE))
                before = self.snapshot()
                # Patch through the OTHER CPU and a distinct main-RAM mirror.
                self.poke(1 - cpu, address + 0x00400000,
                          arm(0xE3A02066, 0xE3A03077), view=1)
                self.assertEqual(self.snapshot(), before)
                self.step(cpu, 2)
                self.assertEqual(self.bridge.get_registers(cpu)[2:4], [0x66, 0x77])

    def test_thumb_prefetch_on_both_cpus_at_both_word_alignments(self) -> None:
        for cpu in (0, 1):
            for alignment in (0, 2):
                with self.subTest(cpu=cpu, alignment=alignment):
                    address = 0x02009000 + cpu * 0x100 + alignment
                    self.set_program(cpu, address, thumb(0x2211, 0x2322, 0x2433, 0xE7FE),
                                     is_thumb=True)
                    before = self.snapshot()
                    self.poke(1 - cpu, address + 0x00800000,
                              thumb(0x2266, 0x2377, 0x2488), view=1)
                    self.assertEqual(self.snapshot(), before)
                    self.step(cpu, 3)
                    self.assertEqual(self.bridge.get_registers(cpu)[2:5], [0x66, 0x77, 0x88])

    def test_one_patch_updates_both_cpus_pending_aliases(self) -> None:
        address = 0x0200A000
        original = arm(0xE3A02011, 0xEAFFFFFE)
        self.set_program(0, address, original)
        self.set_program(1, address + 0x00400000, original)
        before = self.snapshot()
        self.poke(1, address + 0x00C00000, arm(0xE3A02066), view=1)
        self.assertEqual(self.snapshot(), before)
        self.step(0, 1)
        self.step(1, 1)
        self.assertEqual([self.bridge.get_registers(cpu)[2] for cpu in (0, 1)], [0x66, 0x66])

    def test_data_poke_intentionally_keeps_prefetched_instructions(self) -> None:
        for cpu in (0, 1):
            for is_thumb in (False, True):
                with self.subTest(cpu=cpu, thumb=is_thumb):
                    address = 0x0200B000 + cpu * 0x100 + int(is_thumb) * 0x20
                    old = thumb(0x2211, 0xE7FE) if is_thumb else arm(0xE3A02011, 0xEAFFFFFE)
                    new = thumb(0x2266) if is_thumb else arm(0xE3A02066)
                    self.set_program(cpu, address, old, is_thumb=is_thumb)
                    before = self.snapshot()
                    self.poke(cpu, address, new, view=0)
                    self.assertEqual(self.snapshot(), before)
                    self.assertEqual(self.peek(cpu, address, len(new), code=True), new)
                    self.step(cpu, 1)
                    self.assertEqual(self.bridge.get_registers(cpu)[2], 0x11,
                                     "data poke is deliberately NOT a coherent code patch")

    def test_itcm_code_patch_matches_physical_mirrors(self) -> None:
        for is_thumb in (False, True):
            with self.subTest(thumb=is_thumb):
                base = 0x00001000 + int(is_thumb) * 0x20
                old = thumb(0x2211, 0x2322, 0xE7FE) if is_thumb else arm(
                    0xE3A02011, 0xE3A03022, 0xEAFFFFFE)
                new = thumb(0x2266, 0x2377) if is_thumb else arm(0xE3A02066, 0xE3A03077)
                self.set_program(0, base + 0x01000000, old, is_thumb=is_thumb)
                before = self.snapshot()
                self.poke(0, base + 0x8000, new, view=1)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.peek(0, base, len(new), code=True), new)
                self.step(0, 2)
                self.assertEqual(self.bridge.get_registers(0)[2:4], [0x66, 0x77])

    def test_dtcm_poke_is_observed_by_real_cpu_load(self) -> None:
        address, marker = 0x03000020, 0x89ABCDEF
        self.api.melonds_memory_write8(0, WRAM_CONTROL, 0)
        self.api.melonds_memory_write32(0, address, 0x76543210)
        before = self.snapshot()
        self.poke(0, address, arm(marker), view=0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.peek(0, address, 4), arm(marker))
        self.assertEqual(self.peek(0, address, 4, code=True), arm(0x76543210))
        self.run_arm9([0xE5903000], {0: address})  # ldr r3,[r0]
        self.assertEqual(self.bridge.get_registers(0)[3], marker)

    def test_code_view_does_not_overwrite_dtcm_overlay(self) -> None:
        address = 0x03000080
        self.api.melonds_memory_write8(0, WRAM_CONTROL, 0)
        self.poke(0, address, arm(0xDEADBEEF), view=0)
        self.set_program(0, address, arm(0xE3A02011, 0xEAFFFFFE))
        before = self.snapshot()
        self.poke(0, address, arm(0xE3A02066), view=1)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.peek(0, address, 4), arm(0xDEADBEEF))
        self.assertEqual(self.peek(0, address, 4, code=True), arm(0xE3A02066))
        self.step(0, 1)
        self.assertEqual(self.bridge.get_registers(0)[2], 0x66)
        self.assertEqual(self.peek(0, address, 4), arm(0xDEADBEEF))

    def test_dtcm_relocation_and_disable_change_only_data_view(self) -> None:
        address = 0x027C0020
        self.poke(1, address, arm(0x12345678))
        self.run_arm9([0xEE090F11], {0: 0x027C000A})  # DTCM base/size MCR.
        before = self.snapshot()
        self.poke(0, address, arm(0xABCDEF01))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.peek(0, address, 4), arm(0xABCDEF01))
        self.assertEqual(self.peek(0, address, 4, code=True), arm(0x12345678))
        self.run_arm9([0xE5903000], {0: address})
        self.assertEqual(self.bridge.get_registers(0)[3], 0xABCDEF01)
        self.run_arm9([0xEE010F10], {0: 0x00002078})  # Disable both TCMs.
        self.assertEqual(self.peek(0, address, 4), arm(0x12345678))
        self.poke(0, address, arm(0x55667788))
        self.assertEqual(self.peek(1, address, 4), arm(0x55667788))

    def test_maximum_write_ram_mirrors_and_arm7_private_wram(self) -> None:
        payload = bytes((index * 17 + 3) & 0xFF for index in range(4096))
        before = self.snapshot()
        self.poke(1, 0x02C0C000, payload)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.peek(0, 0x0200C000, 4096), payload)
        self.assertEqual(self.peek(1, 0x0280C000, 4096, code=True), payload)
        self.api.melonds_memory_write8(0, WRAM_CONTROL, 0)
        self.poke(1, 0x03000100, arm(0xAABBCCDD))  # Shared unmapped -> private.
        self.assertEqual(self.peek(1, 0x03810100, 4), arm(0xAABBCCDD))
        self.api.melonds_memory_write8(0, WRAM_CONTROL, 3)
        self.poke(1, 0x03000080, arm(0x11223344))
        self.poke(1, 0x03004080, arm(0x55667788))
        self.api.melonds_memory_write8(0, WRAM_CONTROL, 1)
        self.assertEqual(self.peek(1, 0x03000080, 4), arm(0x11223344))
        self.assertEqual(self.peek(0, 0x03004080, 4), arm(0x55667788))
        self.api.melonds_memory_write8(0, WRAM_CONTROL, 2)
        self.assertEqual(self.peek(1, 0x03000080, 4), arm(0x55667788))
        self.assertEqual(self.peek(0, 0x03004080, 4), arm(0x11223344))

    def test_rejections_are_atomic_and_leave_execution_unchanged(self) -> None:
        self.api.melonds_memory_write8(0, WRAM_CONTROL, 0)
        address = 0x0200E000
        self.set_program(0, address, arm(0xE3A02011, 0xEAFFFFFE))
        self.poke(0, 0x03FFFFFE, b"\x12\x34")
        before = self.snapshot()
        prefix = self.peek(0, 0x03FFFFFE, 2)
        bios9, bios7 = self.peek(0, 0xFFFF0000, 4), self.peek(1, 0, 4)
        source = (ctypes.c_uint8 * 8192)(*[0xA5] * 8192)
        requests = [(-1, address, 4, 0), (2, address, 4, 1),
                    (0, address, 4, -1), (0, address, 4, 2),
                    (0, address, 0, 0), (0, address, 4097, 1),
                    (0, 0xFFFFFFF0, 32, 0), (1, 0xFFFFFFFF, 2, 1)]
        for view in (0, 1):
            requests.extend([(0, 0x03FFFFFE, 4, view), (0, 0xFFFF0000, 4, view),
                             (1, 0, 4, view), (1, 0x3FFF, 2, view)])
            for cpu in (0, 1):
                for rejected_address in (0x04000000, 0x04100000, 0x05000000,
                                         0x06000000, 0x07000000, 0x08000000, 0x0A000000):
                    requests.append((cpu, rejected_address, 4, view))
        for cpu, target, length, view in requests:
            with self.subTest(cpu=cpu, address=hex(target), length=length, view=view):
                self.assertEqual(self.api.melonds_memory_poke_block(cpu, target, length, source, view), 0)
        self.assertEqual(self.api.melonds_memory_poke_block(0, address, 4, None, 0), 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.peek(0, 0x03FFFFFE, 2), prefix)
        self.assertEqual(self.peek(0, 0xFFFF0000, 4), bios9)
        self.assertEqual(self.peek(1, 0, 4), bios7)
        self.assertEqual(self.peek(0, address, 4, code=True), arm(0xE3A02011))
        self.step(0, 1)
        self.assertEqual(self.bridge.get_registers(0)[2], 0x11)

    def test_code_peek_rejects_partial_ranges_without_output_or_events(self) -> None:
        self.api.melonds_memory_write8(0, WRAM_CONTROL, 0)
        self.assertGreater(self.api.melonds_debug_wp_add(0, 0x02008000, 16, 3), 0)
        before = self.snapshot()
        self.assertEqual(self.bridge.wp_events(), [])
        self.peek(0, 0x02008000, 16, code=True)
        self.poke(0, 0x02008000, arm(0x12345678), view=0)
        self.poke(0, 0x02008000, arm(0xE3A02066), view=1)
        for cpu, address, length in [(0, 0x03FFFFFE, 4), (0, 0xFFFF0FFE, 4),
                                     (1, 0x3FFF, 2), (0, 0x04100000, 4),
                                     (0, 0xFFFFFFF0, 32), (2, 0x02000000, 4),
                                     (0, 0x02000000, 0), (0, 0x02000000, 4097)]:
            output = (ctypes.c_uint8 * 8192)(*[0xA5] * 8192)
            self.assertEqual(self.api.melonds_code_peek_block(cpu, address, length, output), 0)
            self.assertEqual(bytes(output), b"\xA5" * 8192)
        self.assertEqual(self.api.melonds_code_peek_block(0, 0x02000000, 4, None), 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.bridge.wp_events(), [])

    def test_uninitialized_emulator_is_rejected(self) -> None:
        self.api.melonds_free()
        data = (ctypes.c_uint8 * 4)(1, 2, 3, 4)
        self.assertEqual(self.api.melonds_memory_poke_block(0, 0x02000000, 4, data, 0), 0)
        self.assertEqual(self.api.melonds_memory_poke_block(0, 0x02000000, 4, data, 1), 0)
        self.assertEqual(self.api.melonds_code_peek_block(0, 0x02000000, 4, data), 0)
        self.assertEqual(bytes(data), b"\x01\x02\x03\x04")


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
            print(f"FAIL: native poke child exceeded {args.timeout:g} seconds", file=sys.stderr)
            return 124
    NativeMemoryPoke.library_path = args.library
    names = args.test or unittest.defaultTestLoader.getTestCaseNames(NativeMemoryPoke)
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.TestSuite(NativeMemoryPoke(name) for name in names))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
