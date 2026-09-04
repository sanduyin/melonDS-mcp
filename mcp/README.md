# melonDS-mcp

基于 [melonDS](https://github.com/melonDS-emu/melonDS) 的本地分支（`mcp` 分支），集成完整 MCP（Model Context Protocol）功能，使 AI agent 能够控制模拟器、调试游戏程序并可视化运行状态。

参考并扩展了 [MelonMCP](https://github.com/claudeopusworkspace/MelonMCP) 的架构（shim + FastMCP 模式），在其基础上新增了指令级调试能力。

## 功能模块

| 模块 | 能力 |
|---|---|
| **操控系统** | ROM 加载、暂停/恢复/重置、按键模拟（12 键）、触控映射、翻盖控制、帧推进、条件等待、savestate |
| **调试工具集** | 双 CPU 内存读写、完整寄存器监控（含分组寄存器）、反汇编、PC 断点、数据观察点、指令追踪（64K 环形缓冲）、单步执行 |
| **状态查看** | 运行状态汇总、实测帧率与模拟速度、宿主资源占用、屏幕截图（PNG）、ROM 头信息、游戏变量观察（watch） |

完整工具清单见 [docs/API.md](docs/API.md)。

## 目录结构

```
mcp/
├── CMakeLists.txt        # shim 构建入口（链接 melonDS 核心）
├── shim/
│   ├── mcp_shim.cpp      # C API 层（ctypes 绑定目标）
│   └── platform_stubs.cpp# 无头运行 Platform 实现
├── python/
│   ├── requirements.txt
│   └── melonds_mcp/      # FastMCP 服务器
│       ├── server.py     # 服务器入口
│       ├── emulator.py   # 模拟器状态管理/JIT互锁/帧率统计
│       ├── libmelonds.py # ctypes 绑定
│       ├── tools_control.py  # 操控系统
│       ├── tools_debug.py    # 调试工具集
│       └── tools_status.py   # 状态查看组件
├── scripts/build.sh      # 一键构建
└── docs/API.md           # API 文档
```

核心侧改动（相对上游 melonDS）：

- `src/MCPDebug.h/.cpp` — 新增：断点/观察点/追踪/单步核心逻辑
- `src/ARM.cpp` — 解释器循环与 ARM7 数据访问插入 MCPDebug 钩子
- `src/CP15.cpp` — ARM9 数据访问插入观察点钩子
- `src/NDS.cpp` — RunFrame 帧循环加入断点命中检查：命中后冻结模拟器并立即返回，等待 `continue_after_break` 确认后恢复（未确认前模拟器保持暂停）
- `src/GPU.h/.cpp` — 新增 `SkipRender` 标志（无头多帧提速）

## 构建与运行

依赖：CMake ≥ 3.16、C++17 编译器、**Python ≥ 3.10**（`mcp` 包要求 3.10+；macOS 自带的 3.9 无法使用，请用 Homebrew 的 python3.12/3.13）

> 注意：Python 依赖固定 `mcp>=1.26,<2`（mcp 2.x 将 FastMCP 改名为 MCPServer，API 不兼容）。

```bash
# 构建 libmelonds_mcp（macOS 产出 .dylib，Linux 产出 .so）
./mcp/scripts/build.sh

# 或手动：
cmake -B build -S mcp -DCMAKE_BUILD_TYPE=Release -DENABLE_JIT=ON
cmake --build build -j

# 安装 Python 依赖（建议使用虚拟环境）
cd mcp/python
python3.12 -m venv .venv            # 或 uv venv --python python3.12 .venv
.venv/bin/pip install -r requirements.txt

# 启动 MCP 服务器（stdio）
PYTHONPATH=mcp/python .venv/bin/python -m melonds_mcp
```

### 快速验证（不依赖 MCP 客户端）

```bash
PYTHONPATH=mcp/python python3 - <<'EOF'
from melonds_mcp.emulator import EmulatorState
emu = EmulatorState()
emu.ensure_init()
print(emu.advance_frames(120))     # 无 ROM 也能跑固件启动画面
print(emu.status_summary())
EOF
```

### 接入 MCP 客户端

```json
{
  "mcpServers": {
    "melonds": {
      "command": "python3",
      "args": ["-m", "melonds_mcp"],
      "env": {
        "PYTHONPATH": "/绝对路径/melonDS/mcp/python",
        "MELONDS_MCP_LIB": "/绝对路径/melonDS/build/libmelonds_mcp.dylib",
        "MELONDS_MCP_ROM": "/可选/自动加载的ROM.nds"
      }
    }
  }
}
```

## 许可证

melonDS 采用 GPLv3，本分支同样以 GPLv3 发布。
