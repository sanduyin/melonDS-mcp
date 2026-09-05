"""Real melonDS shared-library smoke/regression tests with a generated ROM.

Usage:
  python mcp/tests/native_smoke.py --library build/mcp-direct/melonds_mcp.dll
  python mcp/tests/native_smoke.py --library <path> --test test_arm7_breakpoint

The default entry point launches a bounded child process: a faulty core hook
cannot hang the invoking test runner forever. No MCP service, commercial ROM,
external BIOS, pytest, or image library is needed.
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TEST_DIR.parent / "python"))

from melonds_mcp.libmelonds import LibMelonDS
from synthetic_rom import CODE_BASES, COUNTERS, REDIRECT_OFFSET, write_rom


class NativeSmoke(unittest.TestCase):
    library_path: Path

    def setUp(self) -> None:
        self.workspace = tempfile.TemporaryDirectory(prefix="melonds-native-smoke-")
        self.addCleanup(self.workspace.cleanup)
        self.rom = write_rom(Path(self.workspace.name) / "自制测试.nds")
        self.bridge = LibMelonDS(self.library_path)
        self.api = self.bridge.lib
        self.assertEqual(self.api.melonds_init(), 0)
        self.addCleanup(self.api.melonds_free)
        # Debugger tables intentionally survive runtime resets, so isolate tests.
        self.api.melonds_debug_bp_clear(-1)
        self.api.melonds_debug_wp_clear(-1)
        self.api.melonds_debug_trace_stop()
        self.api.melonds_debug_break_ack()
        self.assertEqual(self.api.melonds_open(str(self.rom).encode("utf-8")), 1)
        self.assertEqual(self.bridge.get_status()[3], 0, "baseline must interpret")

    def frames(self) -> int:
        return self.bridge.get_status()[1]

    def count(self, cpu: int) -> int:
        return self.api.melonds_memory_read32(cpu, COUNTERS[cpu])

    def full_frame(self) -> None:
        before = self.frames()
        self.assertEqual(self.api.melonds_cycle(), 0)
        self.assertEqual(self.frames(), before + 1)

    def step(self, cpu: int, count: int = 1) -> dict:
        self.api.melonds_debug_step_request(cpu, count)
        self.api.melonds_resume()
        self.assertEqual(self.api.melonds_cycle(), 1)
        hit = self.bridge.break_info()
        self.assertTrue(hit["hit"], hit)
        self.assertEqual((hit["cpu"], hit["reason"]), (cpu, 3), hit)
        self.assertEqual(self.api.melonds_debug_step_pending(), 0)
        return hit

    def test_boot_dual_cpu_counters_and_exact_frames(self) -> None:
        info = self.bridge.rom_info()
        self.assertIsNotNone(info)
        self.assertEqual(info["title"], "MCP SMOKE")
        self.assertEqual(info["code"], "####")
        self.assertEqual(info["arm9_entry"], CODE_BASES[0])
        self.assertEqual(info["arm7_entry"], CODE_BASES[1])
        initial_frames = self.frames()
        self.assertEqual(initial_frames, 0, "a newly loaded ROM starts at frame zero")
        previous = [self.count(0), self.count(1)]
        for frame in range(1, 4):
            self.full_frame()
            self.assertEqual(self.frames(), initial_frames + frame)
            for cpu in (0, 1):
                value = self.count(cpu)
                self.assertGreater(value, previous[cpu], f"CPU {cpu} did not execute")
                previous[cpu] = value

    def test_pause_freezes_both_cpus_memory_and_clock(self) -> None:
        self.full_frame()
        self.api.melonds_pause()
        snapshot = self.bridge.get_status()
        registers = [self.bridge.get_registers(cpu) for cpu in (0, 1)]
        counters = [self.count(cpu) for cpu in (0, 1)]
        # Arguments select total/frame-relative clocks, not ARM9/ARM7. Mode 1
        # updates a measurement baseline and is intentionally not a pure read.
        cycles = [self.api.melonds_get_cycles(mode) for mode in (0, 2)]
        for _ in range(3):
            self.assertEqual(self.api.melonds_cycle(), 0)
            self.assertEqual(self.bridge.get_status(), snapshot)
            self.assertEqual([self.bridge.get_registers(cpu) for cpu in (0, 1)], registers)
            self.assertEqual([self.count(cpu) for cpu in (0, 1)], counters)
            self.assertEqual([self.api.melonds_get_cycles(mode) for mode in (0, 2)], cycles)
        self.api.melonds_resume()
        self.full_frame()

    def test_ram_roundtrip_visible_to_both_cpus(self) -> None:
        address = 0x02003000
        for cpu in (0, 1):
            with self.subTest(cpu=cpu):
                payload = bytes((value + cpu) & 0xFF for value in range(257))
                self.assertEqual(self.bridge.write_block(cpu, address, payload), len(payload))
                self.assertEqual(self.bridge.read_block(cpu, address, len(payload)), payload)
                self.assertEqual(self.bridge.read_block(1 - cpu, address, len(payload)), payload)

    def test_pc_write_refills_pipeline_for_both_cpus(self) -> None:
        for cpu in (0, 1):
            with self.subTest(cpu=cpu):
                self.api.melonds_debug_break_ack()
                self.assertEqual(self.api.melonds_debug_write_register(cpu, 2, 0), 1)
                target = CODE_BASES[cpu] + REDIRECT_OFFSET
                self.assertEqual(self.api.melonds_debug_write_register(cpu, 15, target), 1)
                hit = self.step(cpu)
                self.assertEqual(self.bridge.get_registers(cpu)[2], 0x66)
                self.assertEqual(hit["pc"], target + 4)
                before = self.bridge.get_registers(cpu)
                self.assertEqual(self.api.melonds_debug_write_register(cpu, 16, 0xD2), 0)
                self.assertEqual(self.bridge.get_registers(cpu), before,
                                 "unsupported CPSR writes must have no side effects")

    def _breakpoint_ack_step_and_continue(self, cpu: int) -> None:
        address = CODE_BASES[cpu] + 8
        bp = self.api.melonds_debug_bp_add(cpu, address)
        self.assertGreater(bp, 0)
        initial_frames = self.frames()
        self.assertEqual(self.api.melonds_cycle(), 1)
        hit = self.bridge.break_info()
        self.assertEqual((hit["cpu"], hit["reason"], hit["id"], hit["pc"]),
                         (cpu, 1, bp, address))
        self.assertEqual(self.frames(), initial_frames,
                         "a partial-frame breakpoint must not fabricate a frame")
        before = [self.bridge.get_registers(index) for index in (0, 1)]
        # A hit is sticky for both cores, including ARM7's catch-up loop.
        self.assertEqual(self.api.melonds_cycle(), 1)
        self.assertEqual([self.bridge.get_registers(index) for index in (0, 1)], before)
        self.api.melonds_debug_break_ack()
        self.assertFalse(self.bridge.break_info()["hit"])
        step_hit = self.step(cpu)
        self.assertEqual(step_hit["pc"], address + 4)
        self.assertEqual(self.bridge.get_registers(cpu)[1], before[cpu][1] + 1)
        self.assertEqual(self.frames(), initial_frames)
        self.assertEqual(self.api.melonds_debug_bp_remove(bp), 1)
        self.api.melonds_debug_break_ack()
        self.api.melonds_resume()
        self.full_frame()
        self.assertGreater(self.count(cpu), 0)

    def test_arm9_breakpoint(self) -> None:
        self._breakpoint_ack_step_and_continue(0)

    def test_arm7_breakpoint(self) -> None:
        self._breakpoint_ack_step_and_continue(1)

    def test_repeated_step_preserves_in_progress_scanline(self) -> None:
        initial_frames = self.frames()
        self.step(0, 10000)
        vcount = self.api.melonds_memory_read16(0, 0x04000006)
        self.assertGreater(vcount, 0, "fixture must first advance beyond scanline zero")
        self.assertEqual(self.frames(), initial_frames)
        for _ in range(5):
            self.step(0)
            after = self.api.melonds_memory_read16(0, 0x04000006)
            self.assertGreaterEqual(after, vcount, "step must not restart GPU.StartFrame")
            self.assertEqual(self.frames(), initial_frames)
            vcount = after

    def test_word_store_hits_one_byte_watchpoint_on_both_cpus(self) -> None:
        for cpu in (0, 1):
            with self.subTest(cpu=cpu):
                self.api.melonds_debug_break_ack()
                wp = self.api.melonds_debug_wp_add(cpu, COUNTERS[cpu] + 1, 1, 2)
                self.assertGreater(wp, 0)
                before = self.frames()
                self.api.melonds_resume()
                self.assertEqual(self.api.melonds_cycle(), 1)
                hit = self.bridge.break_info()
                self.assertEqual((hit["cpu"], hit["reason"], hit["id"]), (cpu, 2, wp))
                self.assertEqual(hit["addr"], COUNTERS[cpu])
                self.assertEqual(hit["pc"], CODE_BASES[cpu] + 12)
                self.assertEqual(self.frames(), before)
                events = self.bridge.wp_events()
                self.assertEqual(len(events), 1)
                event = events[0]
                self.assertEqual((event["cpu"], event["address"], event["size"], event["kind"]),
                                 (cpu, COUNTERS[cpu], 4, 2))
                self.assertEqual(event["pc"], CODE_BASES[cpu] + 12)
                self.assertEqual(event["value"], self.count(cpu))
                self.assertEqual(self.api.melonds_debug_wp_remove(wp), 1)
        self.api.melonds_debug_break_ack()
        self.api.melonds_resume()
        self.full_frame()

    def test_savestate_at_arm7_breakpoint_resumes_partial_frame(self) -> None:
        bp = self.api.melonds_debug_bp_add(1, CODE_BASES[1] + 8)
        self.assertGreater(bp, 0)
        self.assertEqual(self.api.melonds_cycle(), 1)
        stopped_frames = self.frames()
        registers = [self.bridge.get_registers(cpu) for cpu in (0, 1)]
        state_path = Path(self.workspace.name) / "断点状态.mst"
        encoded_path = str(state_path).encode("utf-8")
        self.assertEqual(self.api.melonds_savestate_save(encoded_path), 1)
        self.assertGreater(state_path.stat().st_size, 0)
        self.assertEqual(self.api.melonds_debug_bp_remove(bp), 1)
        self.api.melonds_debug_break_ack()
        self.api.melonds_resume()
        self.full_frame()
        completed_registers = [self.bridge.get_registers(cpu) for cpu in (0, 1)]
        completed_counters = [self.count(cpu) for cpu in (0, 1)]
        completed_image = self.bridge.screenshot_bytes()
        self.assertEqual(self.api.melonds_savestate_load(encoded_path), 1)
        self.assertEqual(self.frames(), stopped_frames)
        # Slots 20/21 expose derived code/data-region diagnostics, not saved
        # architectural state. The completed replay below compares every slot.
        restored = [self.bridge.get_registers(cpu) for cpu in (0, 1)]
        for cpu in (0, 1):
            self.assertEqual(restored[cpu][:20], registers[cpu][:20])
            self.assertEqual(restored[cpu][22:], registers[cpu][22:])
        self.api.melonds_resume()
        self.full_frame()
        self.assertEqual([self.bridge.get_registers(cpu) for cpu in (0, 1)], completed_registers)
        self.assertEqual([self.count(cpu) for cpu in (0, 1)], completed_counters)
        self.assertEqual(self.bridge.screenshot_bytes(), completed_image)

    def test_software_screens_render_red_and_blue_backdrops(self) -> None:
        self.api.melonds_memory_write32(0, 0x04000000, 0x00010000)
        self.api.melonds_memory_write32(0, 0x04001000, 0x00010000)
        self.api.melonds_memory_write16(0, 0x04000304, 0x820F)
        self.api.melonds_memory_write16(0, 0x05000000, 0x001F)
        self.api.melonds_memory_write16(0, 0x05000400, 0x7C00)
        self.full_frame()
        self.full_frame()
        before = self.frames()
        image = self.bridge.screenshot_bytes()
        self.assertEqual(len(image), 256 * 384 * 3)
        # Backdrop 5-bit 31 expands to 6-bit 62, then to 8-bit 251 in the
        # upstream SoftRenderer pipeline (not full-range color normalization).
        self.assertEqual(image[:256 * 192 * 3], b"\xfb\x00\x00" * (256 * 192))
        self.assertEqual(image[256 * 192 * 3:], b"\x00\x00\xfb" * (256 * 192))
        self.assertEqual(self.frames(), before, "capturing must not advance execution")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--test", action="append", default=[],
                        help="Run one named NativeSmoke method (repeatable).")
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
            print(f"FAIL: native smoke child exceeded {args.timeout:g} seconds", file=sys.stderr)
            return 124
    NativeSmoke.library_path = args.library
    names = args.test or unittest.defaultTestLoader.getTestCaseNames(NativeSmoke)
    suite = unittest.TestSuite(NativeSmoke(name) for name in names)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
