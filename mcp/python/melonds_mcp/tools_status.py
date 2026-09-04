"""状态查看组件：运行状态、帧率/性能、截图、ROM 信息、游戏内部状态观察。"""

from __future__ import annotations

import base64
import io
import os

from .constants import SCREEN_WIDTH, SCREEN_HEIGHT
from .emulator import EmulatorState, WATCH_TYPES

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False


def register(mcp, emu: EmulatorState) -> None:
    lib = emu.lib.lib

    # ═══════════════ 运行状态 ═══════════════

    @mcp.tool()
    def get_status() -> dict:
        """获取模拟器完整运行状态：运行标志、帧计数、PC、帧率、JIT、调试状态、ROM 信息。"""
        return emu.status_summary()

    @mcp.tool()
    def get_performance() -> dict:
        """获取性能统计：实测帧率、模拟速度倍率、宿主进程 CPU/内存占用。"""
        perf = {
            "fps": round(emu.fps, 2),
            "emulation_speed": round(emu.emulation_speed, 2),
            "note": "fps 在 advance_frames 调用期间以 1 秒滚动窗口实测",
        }
        if HAS_PSUTIL:
            proc = psutil.Process(os.getpid())
            perf["host"] = {
                "cpu_percent": proc.cpu_percent(interval=0.1),
                "memory_mb": round(proc.memory_info().rss / 1024 / 1024, 1),
            }
        else:
            perf["host"] = None
        return perf

    # ═══════════════ 截图 ═══════════════

    @mcp.tool()
    def screenshot(screen: str = "both", format: str = "png") -> dict:
        """截取模拟器屏幕，返回 base64 编码图像。

        Args:
            screen: top=上屏 bottom=下屏 both=双屏竖排
            format: png | rgb_hex（后者返回原始字节的 hex，便于精确分析）
        """
        if not HAS_PIL and format == "png":
            return {"ok": False, "error": "未安装 Pillow（pip install Pillow）"}

        raw = emu.lib.screenshot_bytes()
        top = raw[:SCREEN_WIDTH * SCREEN_HEIGHT * 3]
        bottom = raw[SCREEN_WIDTH * SCREEN_HEIGHT * 3:]

        if format == "rgb_hex":
            data = {"top": "both", "bottom": "both"}
            if screen == "top":
                data = {"top": top.hex()}
            elif screen == "bottom":
                data = {"bottom": bottom.hex()}
            else:
                data = {"top": top.hex(), "bottom": bottom.hex()}
            return {"ok": True, "format": "rgb_hex", "size": {
                "width": SCREEN_WIDTH,
                "height": SCREEN_HEIGHT if screen != "both" else SCREEN_HEIGHT * 2,
            }, "data": data}

        if screen == "top":
            img = Image.frombytes("RGB", (SCREEN_WIDTH, SCREEN_HEIGHT), top)
        elif screen == "bottom":
            img = Image.frombytes("RGB", (SCREEN_WIDTH, SCREEN_HEIGHT), bottom)
        else:
            combined = top + bottom
            img = Image.frombytes("RGB", (SCREEN_WIDTH, SCREEN_HEIGHT * 2), combined)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return {"ok": True, "format": "png", "screen": screen,
                "size": {"width": img.width, "height": img.height},
                "data_base64": b64}

    # ═══════════════ ROM 信息 ═══════════════

    @mcp.tool()
    def get_rom_info() -> dict:
        """获取当前 ROM 头信息（标题/游戏代码/厂商码/入口地址等）。"""
        info = emu.lib.rom_info()
        if info is None:
            return {"ok": False, "error": "未插入 ROM"}
        return {"ok": True, **info}

    # ═══════════════ 游戏内部状态观察（watch）═══════════════

    @mcp.tool()
    def watch_add(label: str, address: int, wtype: str = "u32",
                  cpu: int = 0, length: int = 16) -> dict:
        """添加游戏内部状态观察项（每次查询时读取内存并解析为指定类型）。

        典型用途：监视 HP/金币/坐标等游戏变量。

        Args:
            label: 观察项名称（如 "player_hp"）
            address: 内存地址
            wtype: u8 u16 u32 s8 s16 s32 float ascii
            cpu: 0=ARM9 1=ARM7
            length: ascii 类型的字符串长度
        """
        try:
            w = emu.add_watch(label, cpu, address, wtype, length)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "id": w.id, "label": w.label, "address": w.address,
                "type": w.type, "cpu": w.cpu}

    @mcp.tool()
    def watch_remove(watch_id: int) -> dict:
        """按 ID 删除观察项。"""
        ok = emu.remove_watch(watch_id)
        return {"ok": ok, "id": watch_id}

    @mcp.tool()
    def watch_list(read_values: bool = True) -> dict:
        """列出全部观察项；默认同时读取当前值（游戏状态可视化）。

        Args:
            read_values: 是否读取当前值
        """
        if read_values:
            return {"watches": emu.read_all_watches()}
        return {"watches": [
            {"id": w.id, "label": w.label, "cpu": w.cpu,
             "address": w.address, "type": w.type}
            for w in emu._watches.values()
        ]}

    @mcp.tool()
    def watch_clear() -> dict:
        """清空全部观察项。"""
        n = emu.clear_watches()
        return {"ok": True, "removed": n}

    # ═══════════════ 系统资源 ═══════════════

    @mcp.tool()
    def get_system_info() -> dict:
        """获取模拟系统信息：库版本、模拟周期计数、JIT 状态、屏幕尺寸。"""
        emu.ensure_init()
        return {
            "cycles_arm7": lib.melonds_get_cycles(1),
            "jit_enabled": bool(lib.melonds_jit_enabled()),
            "jit_suppressed_for_debug": emu._jit_suppressed,
            "skip_render": bool(lib.melonds_get_skip_render()),
            "screen": {"width": SCREEN_WIDTH, "height": SCREEN_HEIGHT},
            "watch_types": list(WATCH_TYPES),
            "capabilities": {
                "disassembly": _has_capstone(),
                "png_screenshot": HAS_PIL,
                "host_stats": HAS_PSUTIL,
            },
        }


def _has_capstone() -> bool:
    try:
        import capstone  # noqa: F401
        return True
    except ImportError:
        return False
