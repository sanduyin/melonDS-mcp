"""EmulatorState：模拟器生命周期管理、JIT/调试互锁、帧率统计与内存观察。"""

from __future__ import annotations

import os
import time
import threading
from dataclasses import dataclass, field

from .libmelonds import LibMelonDS
from .constants import CPU_ARM9, CPU_ARM7


@dataclass
class Watch:
    """游戏内部状态观察项（内存监视）。"""

    id: int
    label: str
    cpu: int
    address: int
    type: str  # u8 u16 u32 s8 s16 s32 float ascii
    length: int = 0  # 仅 ascii 使用


WATCH_TYPES = ("u8", "u16", "u32", "s8", "s16", "s32", "float", "ascii")
WATCH_TYPE_SIZES = {"u8": 1, "u16": 2, "u32": 4, "s8": 1, "s16": 2, "s32": 4, "float": 4}


class EmulatorState:
    """持有 libmelonds 实例并管理模拟器状态。

    关键职责：
    - 生命周期：init / open / reset / free
    - JIT/调试互锁：指令级调试功能（断点/单步/追踪/观察点）激活时
      自动切换到解释器模式（钩子仅在解释器中生效），全部停用后恢复 JIT。
    - 帧推进：advance_frames / run_until_break / step
    - 帧率统计：滚动窗口测量实际运行帧率与模拟速度倍率
    - 内存观察（watch）表管理
    """

    def __init__(self):
        # All MCP tool calls share this reentrant lock, including reads. Native
        # calls release the GIL; a GIL alone is not a serialization boundary.
        self.lock = threading.RLock()
        self.lib = LibMelonDS()
        self._initialized = False
        self._rom_path: str | None = None

        # JIT/调试互锁状态
        self._jit_user_pref: bool | None = None   # 用户显式设置的 JIT 偏好
        self._jit_suppressed = False              # 因调试而临时关闭

        # 帧率统计（滚动窗口）
        self._fps_window_start: float = 0.0
        self._fps_window_frames: int = 0
        self.fps: float = 0.0
        self.emulation_speed: float = 0.0  # 相对实时倍率

        # 内存观察
        self._watches: dict[int, Watch] = {}
        self._next_watch_id = 1

    # ── 生命周期 ──

    def ensure_init(self) -> None:
        if not self._initialized:
            rc = self.lib.lib.melonds_init()
            if rc != 0:
                raise RuntimeError("melonds_init 失败")
            self._initialized = True
            # 支持启动时自动加载 ROM
            rom = os.environ.get("MELONDS_MCP_ROM")
            if rom and os.path.exists(rom):
                self.open_rom(rom)

    def open_rom(self, path: str) -> bool:
        self.ensure_init()
        rc = self.lib.lib.melonds_open(path.encode())
        if rc != 1:
            return False
        self._rom_path = path
        self._watches.clear()
        return True

    @property
    def rom_path(self) -> str | None:
        return self._rom_path

    def is_running(self) -> bool:
        return bool(self.lib.lib.melonds_running())

    # ── JIT/调试互锁 ──

    def _sync_jit(self) -> None:
        """根据调试功能激活状态切换 JIT/解释器。"""
        lib = self.lib.lib
        hooks = lib.melonds_debug_hooks_active() or lib.melonds_debug_data_hooks_active()

        if hooks:
            if not self._jit_suppressed and lib.melonds_jit_enabled():
                # 记录用户偏好后临时关闭
                self._jit_user_pref = True
                lib.melonds_set_jit(0)
                self._jit_suppressed = True
        else:
            if self._jit_suppressed:
                lib.melonds_set_jit(1 if self._jit_user_pref is not False else 0)
                self._jit_suppressed = False

    def set_jit(self, enabled: bool) -> bool:
        """用户显式设置 JIT 偏好（调试功能激活期间会被暂时压制）。"""
        self._jit_user_pref = enabled
        if not self._jit_suppressed:
            return bool(self.lib.lib.melonds_set_jit(1 if enabled else 0))
        return False

    # ── 帧推进 ──

    def _cycle(self) -> int:
        """推进一帧，返回 1 表示触发调试暂停。"""
        return self.lib.lib.melonds_cycle()

    def _frame_count(self) -> int:
        return self.lib.get_status()[1]

    def advance_frames(self, n: int, skip_render: bool = False) -> dict:
        """推进 n 帧，统计帧率。返回运行摘要（含断点命中信息）。"""
        self.ensure_init()
        self._sync_jit()
        lib = self.lib.lib

        # Rendering is on by default, so screenshots observe the last completed
        # frame. Mid-frame debugger stops are reported without inventing a frame.
        lib.melonds_set_skip_render(1 if skip_render else 0)

        t0 = time.perf_counter()
        frames_done = 0
        break_hit = False
        try:
            for _ in range(n):
                before = self._frame_count()
                rc = self._cycle()
                advanced = (self._frame_count() - before) & 0xFFFFFFFF
                frames_done += advanced
                if rc == 1:
                    break_hit = True
                    break
                if advanced == 0:
                    break
        finally:
            lib.melonds_set_skip_render(0)

        elapsed = time.perf_counter() - t0

        # 帧率滚动窗口（1 秒）
        now = time.perf_counter()
        if self._fps_window_start == 0.0:
            self._fps_window_start = now
            self._fps_window_frames = 0
        self._fps_window_frames += frames_done
        window = now - self._fps_window_start
        if window >= 1.0:
            self.fps = self._fps_window_frames / window
            from .constants import DS_FRAMERATE
            self.emulation_speed = self.fps / DS_FRAMERATE
            self._fps_window_start = now
            self._fps_window_frames = 0

        result = {
            "frames_executed": frames_done,
            "frames_requested": n,
            "elapsed_seconds": round(elapsed, 4),
            "break_hit": break_hit,
            "frame_number": self._frame_count(),
            "running": self.is_running(),
        }
        if break_hit:
            result["break_info"] = self.lib.break_info()
        return result

    def run_until_break(self, max_frames: int = 3600) -> dict:
        """持续推进直到断点/观察点/单步命中或达到帧上限。"""
        result = self.advance_frames(max_frames)
        result["max_frames"] = max_frames
        result.setdefault("break_info", None)
        return result

    def step(self, cpu: int, count: int) -> dict:
        """单步执行：请求执行 count 条指令后暂停，返回命中信息。"""
        self.ensure_init()
        lib = self.lib.lib

        lib.melonds_debug_step_request(cpu, count)
        self._sync_jit()
        lib.melonds_set_skip_render(0)

        # 持续推进直到命中（最多推进若干帧避免死循环）
        frames = 0
        hit = False
        for _ in range(600):
            before = self._frame_count()
            rc = self._cycle()
            advanced = (self._frame_count() - before) & 0xFFFFFFFF
            frames += advanced
            if rc == 1:
                hit = True
                break
            if not lib.melonds_debug_step_pending():
                # 步进目标已达成但未在本帧内触发（例如 CPU 停机）
                break
            if advanced == 0:
                break

        return {
            "cpu": cpu,
            "instructions": count,
            "instructions_requested": count,
            "frames_executed": frames,
            "hit": hit,
            "break_info": self.lib.break_info() if hit else None,
        }

    def continue_after_break(self) -> None:
        """确认命中，清除暂停状态以继续执行。"""
        self.lib.lib.melonds_debug_break_ack()

    # ── 内存观察（游戏状态可视化）──

    def add_watch(self, label: str, cpu: int, address: int, wtype: str,
                  length: int = 0) -> Watch:
        if wtype not in WATCH_TYPES:
            raise ValueError(f"未知观察类型 {wtype!r}，有效值: {WATCH_TYPES}")
        if cpu not in (CPU_ARM9, CPU_ARM7):
            raise ValueError("cpu 必须为 0 (ARM9) 或 1 (ARM7)")
        if wtype == "ascii" and length <= 0:
            length = 16

        w = Watch(
            id=self._next_watch_id,
            label=label,
            cpu=cpu,
            address=address,
            type=wtype,
            length=length,
        )
        self._watches[w.id] = w
        self._next_watch_id += 1
        return w

    def remove_watch(self, watch_id: int) -> bool:
        return self._watches.pop(watch_id, None) is not None

    def clear_watches(self) -> int:
        n = len(self._watches)
        self._watches.clear()
        return n

    def read_watch(self, w: Watch):
        """读取一个观察项的当前值。"""
        import struct

        if w.type == "ascii":
            data = self.lib.read_block(w.cpu, w.address, w.length)
            return data.split(b"\x00")[0].decode("ascii", errors="replace")

        size = WATCH_TYPE_SIZES[w.type]
        data = self.lib.read_block(w.cpu, w.address, size)
        if w.type in ("u8", "u16", "u32"):
            return int.from_bytes(data, "little")
        if w.type in ("s8", "s16", "s32"):
            return int.from_bytes(data, "little", signed=True)
        if w.type == "float":
            return struct.unpack("<f", data)[0]
        raise ValueError(w.type)

    def read_all_watches(self) -> list[dict]:
        out = []
        for w in self._watches.values():
            try:
                value = self.read_watch(w)
            except Exception as e:  # noqa: BLE001
                value = f"<错误: {e}>"
            out.append({
                "id": w.id,
                "label": w.label,
                "cpu": w.cpu,
                "address": w.address,
                "type": w.type,
                "value": value,
            })
        return out

    # ── 状态快照 ──

    def status_summary(self) -> dict:
        self.ensure_init()
        lib = self.lib.lib
        st = self.lib.get_status()
        cycles = lib.melonds_get_cycles(0)

        summary = {
            "running": bool(st[0]),
            "frames": st[1],
            "lag_frames": st[2],
            "jit_enabled": bool(st[3]),
            "console_type": "DS" if st[4] == 0 else "DSi",
            "rom_inserted": bool(st[5]),
            "pc_arm9": st[6],
            "pc_arm7": st[7],
            "fps": round(self.fps, 2),
            "emulation_speed": round(self.emulation_speed, 2),
            "system_clock_cycles": cycles,
            "cycles_arm7": cycles,
            "cycle_semantics": "system clock; cycles_arm7 is a compatibility alias",
            "rom": self.lib.rom_info(),
            "rom_path": self._rom_path,
            "debug": {
                "hooks_active": bool(lib.melonds_debug_hooks_active()),
                "data_hooks_active": bool(lib.melonds_debug_data_hooks_active()),
                "step_pending": bool(lib.melonds_debug_step_pending()),
                "trace_active": bool(lib.melonds_debug_trace_active()),
                "break_info": self.lib.break_info(),
                "watches": len(self._watches),
            },
        }
        return summary
