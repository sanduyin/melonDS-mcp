"""melonDS-mcp：基于 melonDS fork 的 MCP 模拟器控制/调试服务器。

模块结构：
- libmelonds   ctypes 绑定（libmelonds_mcp C API）
- constants    按键位掩码与屏幕常量
- emulator     EmulatorState：模拟器生命周期、JIT/调试互锁、FPS 统计
- server       FastMCP 服务器入口
- tools_control / tools_debug / tools_status  三大工具模块
"""

__version__ = "0.1.0"
