"""FastMCP stdio entry point for emulator, debugger, graphics and analysis tools."""

from __future__ import annotations

import sys

from mcp.server.fastmcp import FastMCP

from . import __version__
from .emulator import EmulatorState
from . import tools_control, tools_debug, tools_status, tools_graphics, tools_analysis, tools_memory, tools_save
from .tool_boundary import ToolBoundary


def create_server(emu: EmulatorState | None = None) -> tuple[FastMCP, EmulatorState]:
    """创建 FastMCP 服务器并注册全部工具。"""
    mcp = FastMCP(
        "melonds-mcp",
        instructions=(
            "melonDS 模拟器 MCP 服务器：提供模拟操控（按键/触控/帧推进）、"
            "调试（内存/寄存器/断点/追踪/单步）与状态查看（帧率/截图/游戏变量观察）"
            "以及原始 GPU 资源检查、可选 Ghidra 真正反编译工具。典型流程：load_rom -> advance_frames -> screenshot，"
            "或 breakpoint_add -> run_until_break -> read_registers -> step。"
            "数据使用 memory_peek/memory_poke；代码使用 code_peek/code_patch，写入建议带 expected_hex。"
            "get_pc 是下一指令地址，read_registers.pc 是原始流水线 R15。"
            "GPU tilemap/sprite 返回透明 PNG，物理 bank/offset 需显式指定。"
            "read_memory/write_memory 是显式总线访问，可能影响设备。"
            "制作金手指/改存档：先 save_workspace_prepare，再加载隔离副本；memory_scan/"
            "memory_table_read 核实字段，memory_table_patch 用哈希校验并保留更强标记。"
            "用游戏内保存后 backup_export 普通 .sav，必须冷启动验证；即时存档可能回写旧 .sav。"
            "cheat_generate_ar 只生成代码，不启用；Select 为按住期间重复，不是真正一次性。"
        ),
    )

    emu = emu or EmulatorState()
    boundary = ToolBoundary(mcp, emu)
    tools_control.register(boundary, emu)
    tools_debug.register(boundary, emu)
    tools_status.register(boundary, emu)
    tools_graphics.register(boundary, emu)
    tools_analysis.register(boundary, emu)
    tools_memory.register(boundary, emu)
    tools_save.register(boundary, emu)

    return mcp, emu


def main() -> None:
    mcp, emu = create_server()
    try:
        mcp.run(transport="stdio")
    finally:
        with emu.lock:
            emu.lib.lib.melonds_free()


if __name__ == "__main__":
    main()
