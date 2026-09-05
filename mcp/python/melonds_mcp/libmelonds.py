"""libmelonds_mcp 的 ctypes 绑定层。

加载 melonds_mcp.dll / libmelonds_mcp.dylib / .so 并声明全部 C API 签名。
库路径查找顺序：
1. 环境变量 MELONDS_MCP_LIB
2. 仓库根目录 build/ 下
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

from .constants import REG_BUFFER_SIZE


def _candidate_paths() -> list[Path]:
    candidates = []
    env = os.environ.get("MELONDS_MCP_LIB")
    if env:
        candidates.append(Path(env))

    # 仓库根目录: mcp/python/melonds_mcp/libmelonds.py -> 上溯 4 级
    here = Path(__file__).resolve()
    repo_root = here.parents[3]
    build_dir = repo_root / "build"

    names = (["melonds_mcp.dll", "libmelonds_mcp.dll"] if sys.platform == "win32"
             else ["libmelonds_mcp.dylib"] if sys.platform == "darwin"
             else ["libmelonds_mcp.so"])
    for directory in (build_dir / "mcp-direct", build_dir / "mcp-direct" / "Release",
                      build_dir / "mcp", build_dir / "mcp" / "Release",
                      build_dir, build_dir / "Release"):
        candidates.extend(directory / name for name in names)
    return candidates


def find_library() -> Path:
    for p in _candidate_paths():
        if p.is_file():
            return p.resolve()
    raise FileNotFoundError(
        "找不到 libmelonds_mcp 库。请先运行 mcp/scripts/build.sh，"
        "或通过环境变量 MELONDS_MCP_LIB 指定路径。"
    )


class LibMelonDS:
    """libmelonds_mcp 的 ctypes 封装。"""

    def __init__(self, libpath: Path | None = None):
        self.path = libpath or find_library()
        self._dll_directory = (os.add_dll_directory(str(self.path.resolve().parent))
                               if sys.platform == "win32" else None)
        self.lib = ctypes.CDLL(str(self.path))
        self._declare()

    def _declare(self) -> None:
        lib = self.lib

        # ── 生命周期 ──
        lib.melonds_init.restype = ctypes.c_int
        lib.melonds_free.restype = None
        lib.melonds_open.argtypes = [ctypes.c_char_p]
        lib.melonds_open.restype = ctypes.c_int
        lib.melonds_pause.restype = None
        lib.melonds_resume.restype = None
        lib.melonds_reset.restype = None
        lib.melonds_running.restype = ctypes.c_int
        lib.melonds_cycle.restype = ctypes.c_int

        # ── 显示 ──
        lib.melonds_screenshot.argtypes = [ctypes.c_char_p]
        lib.melonds_screenshot.restype = None

        # Direct copies from physical GPU storage, without CPU MMIO access.
        lib.melonds_gpu_read.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint32,
                                        ctypes.POINTER(ctypes.c_ubyte), ctypes.c_uint32]
        lib.melonds_gpu_read.restype = ctypes.c_uint32
        lib.melonds_gpu_state.argtypes = [ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint32]
        lib.melonds_gpu_state.restype = ctypes.c_int

        # ── 输入 ──
        lib.melonds_input_keypad_update.argtypes = [ctypes.c_ushort]
        lib.melonds_input_keypad_update.restype = None
        lib.melonds_input_keypad_get.restype = ctypes.c_ushort
        lib.melonds_input_set_touch_pos.argtypes = [ctypes.c_ushort, ctypes.c_ushort]
        lib.melonds_input_set_touch_pos.restype = None
        lib.melonds_input_release_touch.restype = None
        lib.melonds_set_lid_closed.argtypes = [ctypes.c_int]
        lib.melonds_set_lid_closed.restype = None
        lib.melonds_get_lid_closed.restype = ctypes.c_int

        # ── Savestate ──
        lib.melonds_savestate_save.argtypes = [ctypes.c_char_p]
        lib.melonds_savestate_save.restype = ctypes.c_int
        lib.melonds_savestate_load.argtypes = [ctypes.c_char_p]
        lib.melonds_savestate_load.restype = ctypes.c_int
        lib.melonds_savestate_slot_save.argtypes = [ctypes.c_int]
        lib.melonds_savestate_slot_save.restype = None
        lib.melonds_savestate_slot_load.argtypes = [ctypes.c_int]
        lib.melonds_savestate_slot_load.restype = None
        lib.melonds_savestate_slot_exists.argtypes = [ctypes.c_int]
        lib.melonds_savestate_slot_exists.restype = ctypes.c_int

        # ── 内存（cpu: 0=ARM9 1=ARM7）──
        u32 = ctypes.c_uint
        lib.melonds_memory_read8.argtypes = [ctypes.c_int, u32]
        lib.melonds_memory_read8.restype = ctypes.c_ubyte
        lib.melonds_memory_read16.argtypes = [ctypes.c_int, u32]
        lib.melonds_memory_read16.restype = ctypes.c_ushort
        lib.melonds_memory_read32.argtypes = [ctypes.c_int, u32]
        lib.melonds_memory_read32.restype = u32
        lib.melonds_memory_read_block.argtypes = [ctypes.c_int, u32, ctypes.c_int, ctypes.c_char_p]
        lib.melonds_memory_read_block.restype = ctypes.c_int
        # Optional for compatibility with builds predating the safe debug view.
        peek = getattr(lib, "melonds_memory_peek_block", None)
        if peek is not None:
            peek.argtypes = [ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32,
                             ctypes.POINTER(ctypes.c_ubyte)]
            peek.restype = ctypes.c_uint32
        code_peek = getattr(lib, "melonds_code_peek_block", None)
        if code_peek is not None:
            code_peek.argtypes = [ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32,
                                  ctypes.POINTER(ctypes.c_ubyte)]
            code_peek.restype = ctypes.c_uint32
        poke = getattr(lib, "melonds_memory_poke_block", None)
        if poke is not None:
            poke.argtypes = [ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32,
                             ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int]
            poke.restype = ctypes.c_uint32
        lib.melonds_memory_write8.argtypes = [ctypes.c_int, u32, ctypes.c_ubyte]
        lib.melonds_memory_write8.restype = None
        lib.melonds_memory_write16.argtypes = [ctypes.c_int, u32, ctypes.c_ushort]
        lib.melonds_memory_write16.restype = None
        lib.melonds_memory_write32.argtypes = [ctypes.c_int, u32, u32]
        lib.melonds_memory_write32.restype = None
        lib.melonds_memory_write_block.argtypes = [ctypes.c_int, u32, ctypes.c_int, ctypes.c_char_p]
        lib.melonds_memory_write_block.restype = ctypes.c_int

        # ── 调试：寄存器 ──
        lib.melonds_debug_get_registers.argtypes = [ctypes.c_int, ctypes.POINTER(u32)]
        lib.melonds_debug_get_registers.restype = None
        lib.melonds_debug_write_register.argtypes = [ctypes.c_int, ctypes.c_int, u32]
        lib.melonds_debug_write_register.restype = ctypes.c_int
        lib.melonds_get_pc.argtypes = [ctypes.c_int]
        lib.melonds_get_pc.restype = u32

        # ── 调试：断点 ──
        lib.melonds_debug_bp_add.argtypes = [ctypes.c_int, u32]
        lib.melonds_debug_bp_add.restype = ctypes.c_int
        lib.melonds_debug_bp_remove.argtypes = [ctypes.c_int]
        lib.melonds_debug_bp_remove.restype = ctypes.c_int
        lib.melonds_debug_bp_set_enabled.argtypes = [ctypes.c_int, ctypes.c_int]
        lib.melonds_debug_bp_set_enabled.restype = ctypes.c_int
        lib.melonds_debug_bp_clear.argtypes = [ctypes.c_int]
        lib.melonds_debug_bp_clear.restype = None
        lib.melonds_debug_bp_list.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(u32),
            ctypes.POINTER(ctypes.c_ubyte), ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_int,
        ]
        lib.melonds_debug_bp_list.restype = ctypes.c_int

        # ── 调试：观察点 ──
        lib.melonds_debug_wp_add.argtypes = [ctypes.c_int, u32, u32, ctypes.c_int]
        lib.melonds_debug_wp_add.restype = ctypes.c_int
        lib.melonds_debug_wp_remove.argtypes = [ctypes.c_int]
        lib.melonds_debug_wp_remove.restype = ctypes.c_int
        lib.melonds_debug_wp_set_enabled.argtypes = [ctypes.c_int, ctypes.c_int]
        lib.melonds_debug_wp_set_enabled.restype = ctypes.c_int
        lib.melonds_debug_wp_clear.argtypes = [ctypes.c_int]
        lib.melonds_debug_wp_clear.restype = None
        lib.melonds_debug_wp_list.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(u32), ctypes.POINTER(u32),
            ctypes.POINTER(ctypes.c_ubyte), ctypes.POINTER(ctypes.c_ubyte),
            ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int,
        ]
        lib.melonds_debug_wp_list.restype = ctypes.c_int

        # ── 调试：单步 ──
        lib.melonds_debug_step_request.argtypes = [ctypes.c_int, u32]
        lib.melonds_debug_step_request.restype = None
        lib.melonds_debug_step_pending.restype = ctypes.c_int

        # ── 调试：追踪 ──
        lib.melonds_debug_trace_start.argtypes = [ctypes.c_int, u32, u32]
        lib.melonds_debug_trace_start.restype = None
        lib.melonds_debug_trace_stop.restype = None
        lib.melonds_debug_trace_active.restype = ctypes.c_int
        lib.melonds_debug_trace_count.restype = u32
        lib.melonds_debug_trace_drain.argtypes = [ctypes.POINTER(u32), u32]
        lib.melonds_debug_trace_drain.restype = u32

        # ── 调试：命中状态 ──
        lib.melonds_debug_break_info.argtypes = [ctypes.POINTER(u32)]
        lib.melonds_debug_break_info.restype = None
        lib.melonds_debug_break_ack.restype = None
        lib.melonds_debug_wp_events.argtypes = [ctypes.POINTER(u32), u32]
        lib.melonds_debug_wp_events.restype = u32
        lib.melonds_debug_hooks_active.restype = ctypes.c_int
        lib.melonds_debug_data_hooks_active.restype = ctypes.c_int

        # ── 状态 ──
        lib.melonds_get_status.argtypes = [ctypes.POINTER(u32)]
        lib.melonds_get_status.restype = None
        lib.melonds_get_cycles.argtypes = [ctypes.c_int]
        lib.melonds_get_cycles.restype = ctypes.c_ulonglong
        lib.melonds_get_rom_info.argtypes = [
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
            ctypes.POINTER(u32),
        ]
        lib.melonds_get_rom_info.restype = ctypes.c_int

        # ── 音频 ──
        lib.melonds_audio_enable.restype = None
        lib.melonds_audio_disable.restype = None
        lib.melonds_audio_samples_available.restype = ctypes.c_uint
        lib.melonds_audio_read.argtypes = [ctypes.POINTER(ctypes.c_short), ctypes.c_uint]
        lib.melonds_audio_read.restype = ctypes.c_uint

        # ── 存档 ──
        lib.melonds_backup_import.argtypes = [ctypes.c_char_p]
        lib.melonds_backup_import.restype = ctypes.c_int
        lib.melonds_backup_export.argtypes = [ctypes.c_char_p]
        lib.melonds_backup_export.restype = ctypes.c_int

        # ── 渲染跳过 / JIT ──
        lib.melonds_set_skip_render.argtypes = [ctypes.c_int]
        lib.melonds_set_skip_render.restype = None
        lib.melonds_get_skip_render.restype = ctypes.c_int
        lib.melonds_jit_enabled.restype = ctypes.c_int
        lib.melonds_set_jit.argtypes = [ctypes.c_int]
        lib.melonds_set_jit.restype = ctypes.c_int

    # ── 便捷方法 ──

    def get_registers(self, cpu: int) -> list[int]:
        buf = (ctypes.c_uint * REG_BUFFER_SIZE)()
        self.lib.melonds_debug_get_registers(cpu, buf)
        return list(buf)

    def get_status(self) -> list[int]:
        buf = (ctypes.c_uint * 9)()
        self.lib.melonds_get_status(buf)
        return list(buf)

    def break_info(self) -> dict:
        buf = (ctypes.c_uint * 6)()
        self.lib.melonds_debug_break_info(buf)
        return {
            "hit": bool(buf[0]),
            "cpu": buf[1],
            "reason": buf[2],
            "id": buf[3],
            "pc": buf[4],
            "addr": buf[5],
        }

    def read_block(self, cpu: int, address: int, size: int) -> bytes:
        buf = ctypes.create_string_buffer(size)
        n = self.lib.melonds_memory_read_block(cpu, address, size, buf)
        return buf.raw[:n]

    def peek_block(self, cpu: int, address: int, size: int) -> bytes:
        """Side-effect-free DS CPU data view, not instruction cache contents.

        Native code validates the currently mapped RAM/WRAM/TCM/BIOS region.
        Never fall back to the legacy bus read when this API is unavailable or
        refuses a range; such a fallback could acknowledge interrupts or FIFOs.
        The caller must hold the same emulator lock as other native operations.
        """
        return self._debug_peek(cpu, address, size, instruction=False)

    @staticmethod
    def _debug_range(cpu: int, address: int, size: int) -> None:
        if type(cpu) is not int or cpu not in (0, 1):
            raise ValueError("cpu must be 0 (ARM9) or 1 (ARM7)")
        if type(address) is not int or not 0 <= address <= 0xFFFFFFFF:
            raise ValueError("address must be a uint32 integer")
        if type(size) is not int or not 1 <= size <= 4096:
            raise ValueError("size must be an integer in [1, 4096]")
        if address + size > 0x100000000:
            raise ValueError("debug range exceeds the 32-bit address space")

    def code_peek_block(self, cpu: int, address: int, size: int) -> bytes:
        """Instruction backing bytes: ARM9 ITCM overlay but no DTCM overlay.

        This does not read the emulated I-cache or prefetched instruction slots.
        """
        return self._debug_peek(cpu, address, size, instruction=True)

    def _debug_peek(self, cpu: int, address: int, size: int, *, instruction: bool) -> bytes:
        self._debug_range(cpu, address, size)
        symbol = "melonds_code_peek_block" if instruction else "melonds_memory_peek_block"
        peek = getattr(self.lib, symbol, None)
        if peek is None:
            raise RuntimeError(f"native library lacks {symbol}; rebuild the MCP library for safe debug peek")
        buffer = (ctypes.c_ubyte * size)()
        copied = peek(cpu, address, size, buffer)
        if copied != size:
            raise RuntimeError(
                f"debug peek refused or returned a short read: cpu={cpu}, address=0x{address:08x}, "
                f"requested={size}, copied={copied}; only supported DS RAM/WRAM/TCM/BIOS data views are allowed "
                "(no MMIO/GPU/cart/DSi or cross-region reads)"
            )
        return bytes(buffer)

    def poke_block(self, cpu: int, address: int, data: bytes, *, instruction: bool = False) -> int:
        """Atomically modify a supported DS backing range; no MMIO/bus fallback.

        instruction=False follows the data view and leaves prefetch unchanged.
        instruction=True follows instruction backing and refreshes affected
        prefetched bytes/cache state without executing either CPU.
        """
        if type(data) is not bytes:
            raise ValueError("data must be bytes")
        if type(instruction) is not bool:
            raise ValueError("instruction must be a boolean")
        self._debug_range(cpu, address, len(data))
        poke = getattr(self.lib, "melonds_memory_poke_block", None)
        if poke is None:
            raise RuntimeError("native library lacks melonds_memory_poke_block; rebuild the MCP library for safe debug writes")
        source = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        written = poke(cpu, address, len(data), source, int(instruction))
        if written != len(data):
            raise RuntimeError(
                f"debug write refused or returned an invalid length: requested={len(data)}, written={written}; "
                "only supported DS writable RAM/WRAM/TCM backing ranges are allowed"
            )
        return written

    def write_block(self, cpu: int, address: int, data: bytes) -> int:
        return self.lib.melonds_memory_write_block(cpu, address, len(data), data)

    def bp_list(self, cpu: int = -1) -> list[dict]:
        max = 64
        ids = (ctypes.c_int * max)()
        addrs = (ctypes.c_uint * max)()
        cpus = (ctypes.c_ubyte * max)()
        en = (ctypes.c_ubyte * max)()
        n = self.lib.melonds_debug_bp_list(cpu, ids, addrs, cpus, en, max)
        return [
            {"id": ids[i], "address": addrs[i], "cpu": cpus[i], "enabled": bool(en[i])}
            for i in range(n)
        ]

    def wp_list(self, cpu: int = -1) -> list[dict]:
        max = 64
        ids = (ctypes.c_int * max)()
        starts = (ctypes.c_uint * max)()
        ends = (ctypes.c_uint * max)()
        cpus = (ctypes.c_ubyte * max)()
        kinds = (ctypes.c_ubyte * max)()
        en = (ctypes.c_ubyte * max)()
        n = self.lib.melonds_debug_wp_list(cpu, ids, starts, ends, cpus, kinds, en, max)
        return [
            {"id": ids[i], "start": starts[i], "end": ends[i],
             "cpu": cpus[i], "kind": kinds[i], "enabled": bool(en[i])}
            for i in range(n)
        ]

    def trace_drain(self, max_entries: int = 65536) -> list[dict]:
        buf = (ctypes.c_uint * (max_entries * 4))()
        n = self.lib.melonds_debug_trace_drain(buf, max_entries)
        return [
            {
                "cpu": buf[i * 4],
                "pc": buf[i * 4 + 1],
                "instr": buf[i * 4 + 2],
                "cpsr": buf[i * 4 + 3],
            }
            for i in range(n)
        ]

    def wp_events(self, max_events: int = 256) -> list[dict]:
        buf = (ctypes.c_uint * (max_events * 6))()
        n = self.lib.melonds_debug_wp_events(buf, max_events)
        return [
            {
                "cpu": buf[i * 6],
                "address": buf[i * 6 + 1],
                "pc": buf[i * 6 + 2],
                "kind": buf[i * 6 + 3],
                "size": buf[i * 6 + 4],
                "value": buf[i * 6 + 5],
            }
            for i in range(n)
        ]

    def rom_info(self) -> dict | None:
        title = ctypes.create_string_buffer(16)
        code = ctypes.create_string_buffer(8)
        maker = ctypes.create_string_buffer(8)
        sizes = (ctypes.c_uint * 6)()
        ok = self.lib.melonds_get_rom_info(title, code, maker, sizes)
        if not ok:
            return None
        return {
            "title": title.value.decode("ascii", errors="replace"),
            "code": code.value.decode("ascii", errors="replace"),
            "maker": maker.value.decode("ascii", errors="replace"),
            "rom_size": sizes[0],
            "arm9_ram_address": sizes[1],
            "arm9_entry": sizes[2],
            "arm7_ram_address": sizes[3],
            "arm7_entry": sizes[4],
            "banner_offset": sizes[5],
        }

    def screenshot_bytes(self) -> bytes:
        buf = ctypes.create_string_buffer(294912)
        self.lib.melonds_screenshot(buf)
        return buf.raw
