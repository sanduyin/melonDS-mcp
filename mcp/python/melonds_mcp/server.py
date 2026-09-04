"""FastMCP 服务器入口：注册三大工具模块并通过 stdio 运行。"""

from __future__ import annotations

import sys

from mcp.server.fastmcp import FastMCP

from . import __version__
from .emulator import EmulatorState
from . import tools_control, tools_debug, tools_status


def create_server() -> tuple[FastMCP, EmulatorState]:
    """创建 FastMCP 服务器并注册全部工具。"""
    mcp = FastMCP(
        "melonds-mcp",
        instructions=(
            "melonDS 模拟器 MCP 服务器：提供模拟操控（按键/触控/帧推进）、"
            "调试（内存/寄存器/断点/追踪/单步）与状态查看（帧率/截图/游戏变量观察）"
            "三类工具。典型流程：load_rom -> advance_frames -> screenshot，"
            "或 breakpoint_add -> run_until_break -> read_registers -> step。"
        ),
    )

    emu = EmulatorState()
    tools_control.register(mcp, emu)
    tools_debug.register(mcp, emu)
    tools_status.register(mcp, emu)

    return mcp, emu


def main() -> None:
    mcp, _emu = create_server()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
