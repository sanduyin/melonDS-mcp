"""操控系统工具：ROM 加载、运行控制、按键/触控输入、帧推进、savestate。"""

from __future__ import annotations

import os

from .constants import buttons_to_bitmask, SCREEN_WIDTH, SCREEN_HEIGHT, VALID_BUTTONS
from .emulator import EmulatorState


def register(mcp, emu: EmulatorState) -> None:
    lib = emu.lib.lib

    @mcp.tool()
    def load_rom(path: str) -> dict:
        """加载 NDS ROM 并启动模拟（自动从 ROM 同目录读取 .sav 存档）。

        Args:
            path: ROM 文件路径（.nds）
        """
        if not os.path.exists(path):
            return {"ok": False, "error": f"文件不存在: {path}"}
        ok = emu.open_rom(path)
        return {"ok": ok, "rom_path": path} if ok else {"ok": False, "error": "ROM 解析失败"}

    @mcp.tool()
    def pause_emulation() -> dict:
        """暂停模拟（保留当前状态，可随时恢复）。"""
        lib.melonds_pause()
        return {"ok": True, "running": False}

    @mcp.tool()
    def resume_emulation() -> dict:
        """恢复模拟运行。若处于断点暂停状态需先调用 continue_after_break。"""
        lib.melonds_resume()
        return {"ok": True, "running": True}

    @mcp.tool()
    def reset_emulation() -> dict:
        """重置模拟器（回到 ROM 启动状态，调试断点保留）。"""
        lib.melonds_reset()
        return {"ok": True}

    @mcp.tool()
    def is_running() -> dict:
        """查询模拟器当前是否处于运行状态。"""
        return {"running": emu.is_running()}

    @mcp.tool()
    def press_buttons(buttons: list[str], frames: int = 1) -> dict:
        """按下按键并保持指定帧数后释放。

        Args:
            buttons: 按键名列表，如 ["a", "start"]。
                     有效按键: a b x y l r start select up down left right
            frames: 按住持续帧数（默认 1 帧 ≈ 1/60 秒）
        """
        try:
            mask = buttons_to_bitmask(buttons)
        except ValueError as e:
            return {"ok": False, "error": str(e)}

        if frames < 1:
            frames = 1

        emu.ensure_init()
        lib.melonds_input_keypad_update(mask)
        result = emu.advance_frames(frames)
        lib.melonds_input_keypad_update(0)
        result["buttons"] = buttons
        return result

    @mcp.tool()
    def set_buttons(buttons: list[str]) -> dict:
        """设置持续按住的按键（不自动释放，需再次调用以变更）。

        Args:
            buttons: 按键名列表，空列表表示全部释放
        """
        try:
            mask = buttons_to_bitmask(buttons)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        lib.melonds_input_keypad_update(mask)
        return {"ok": True, "buttons": buttons}

    @mcp.tool()
    def get_buttons() -> dict:
        """查询当前按住的按键。"""
        mask = lib.melonds_input_keypad_get()
        pressed = [name for name in VALID_BUTTONS if mask & (1 << VALID_BUTTONS.index(name))]
        return {"mask": mask, "pressed": pressed}

    @mcp.tool()
    def tap_screen(x: int, y: int, frames: int = 1) -> dict:
        """点击下触摸屏指定坐标并保持指定帧数后释放。

        Args:
            x: 触摸 X 坐标 (0-255)
            y: 触摸 Y 坐标 (0-191，相对下屏)
            frames: 按住持续帧数
        """
        if not (0 <= x < SCREEN_WIDTH and 0 <= y < SCREEN_HEIGHT):
            return {"ok": False, "error": f"坐标越界: ({x}, {y})，有效范围 0-255 x 0-191"}

        emu.ensure_init()
        lib.melonds_input_set_touch_pos(x, y)
        result = emu.advance_frames(frames)
        lib.melonds_input_release_touch()
        result["touch"] = {"x": x, "y": y}
        return result

    @mcp.tool()
    def set_touch(x: int, y: int) -> dict:
        """持续按住触摸屏坐标（需调用 release_touch 释放）。"""
        if not (0 <= x < SCREEN_WIDTH and 0 <= y < SCREEN_HEIGHT):
            return {"ok": False, "error": "坐标越界"}
        lib.melonds_input_set_touch_pos(x, y)
        return {"ok": True}

    @mcp.tool()
    def release_touch() -> dict:
        """释放触摸屏。"""
        lib.melonds_input_release_touch()
        return {"ok": True}

    @mcp.tool()
    def set_lid(closed: bool) -> dict:
        """开合翻盖（休眠用）。"""
        lib.melonds_set_lid_closed(1 if closed else 0)
        return {"ok": True, "lid_closed": closed}

    @mcp.tool()
    def advance_frames(frames: int = 1) -> dict:
        """推进模拟指定帧数（跳过渲染以提速，用于等待游戏状态变化）。

        Args:
            frames: 帧数（默认 1）
        """
        if frames < 1:
            frames = 1
        return emu.advance_frames(frames)

    @mcp.tool()
    def advance_frames_until(
        address: int,
        expected: int,
        size: int = 4,
        cpu: int = 0,
        max_frames: int = 3600,
    ) -> dict:
        """推进帧直到指定内存地址的值等于期望值（轮询等待游戏状态）。

        Args:
            address: 内存地址
            expected: 期望值
            size: 读取宽度（1/2/4 字节）
            cpu: 0=ARM9 1=ARM7
            max_frames: 最大推进帧数（默认 3600 ≈ 60 秒）
        """
        emu.ensure_init()
        emu._sync_jit()
        lib.melonds_set_skip_render(1)

        def read() -> int:
            if size == 1:
                return lib.melonds_memory_read8(cpu, address)
            if size == 2:
                return lib.melonds_memory_read16(cpu, address)
            return lib.melonds_memory_read32(cpu, address)

        frames_done = 0
        hit = False
        for _ in range(max_frames):
            rc = emu._cycle()
            frames_done += 1
            if rc == 1:
                hit = True
                break
            if read() == expected:
                break

        lib.melonds_set_skip_render(0)
        return {
            "frames_executed": frames_done,
            "final_value": read(),
            "match": read() == expected and not hit,
            "break_hit": hit,
        }

    @mcp.tool()
    def run_until_break(max_frames: int = 3600) -> dict:
        """持续运行直到命中断点/观察点/单步，或达到帧上限。"""
        return emu.run_until_break(max_frames)

    # ── Savestate ──

    @mcp.tool()
    def savestate_save(path: str = "", slot: int = -1) -> dict:
        """保存 savestate 到文件或 slot（二选一）。

        Args:
            path: 文件路径（.mst）
            slot: slot 编号（1-9），基于 ROM 路径自动命名
        """
        if slot > 0:
            lib.melonds_savestate_slot_save(slot)
            return {"ok": True, "slot": slot}
        if not path:
            return {"ok": False, "error": "需要 path 或 slot 参数"}
        ok = lib.melonds_savestate_save(path.encode())
        return {"ok": bool(ok), "path": path}

    @mcp.tool()
    def savestate_load(path: str = "", slot: int = -1) -> dict:
        """从文件或 slot 加载 savestate。"""
        if slot > 0:
            ok = lib.melonds_savestate_slot_exists(slot)
            if not ok:
                return {"ok": False, "error": f"slot {slot} 不存在"}
            lib.melonds_savestate_slot_load(slot)
            return {"ok": True, "slot": slot}
        if not path:
            return {"ok": False, "error": "需要 path 或 slot 参数"}
        if not os.path.exists(path):
            return {"ok": False, "error": f"文件不存在: {path}"}
        ok = lib.melonds_savestate_load(path.encode())
        return {"ok": bool(ok), "path": path}
