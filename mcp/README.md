# melonDS MCP — headless fork integration

本目录复用 [sanduyin/melonDS-mcp](https://github.com/sanduyin/melonDS-mcp) 的
`3b39290543904bbafe62ec5c4f208b69172212ec` 实现（GPLv3），保留原 48 个工具，
新增 7 个 GPU 检查、3 个 Ghidra 分析、4 个安全内存/代码和6个金手指/普通存档工具，共68个 MCP 工具。
原项目说明其 shim/FastMCP 设计参考了
[MelonMCP](https://github.com/claudeopusworkspace/MelonMCP)。核心和 shim 的来源及改动保留在源码中。

调用路径为 `MCP stdio → Python/ctypes → melonDS shared core`，不依赖 GUI/GDB。
一次工具调用完整串行执行；运行只在帧推进/按键/触控/单步等工具中发生，`resume`
仅恢复可推进状态，不启动后台实时运行。

## 构建和启动

需要 CMake、C++ 编译器和 Python 3.10+。Linux/macOS：

```bash
./mcp/scripts/build.sh
PYTHONPATH="$PWD/mcp/python" mcp/.venv/bin/python -m melonds_mcp
```

Windows，脚本优先使用现有 Developer Shell，也会自动发现 VS C++ Build Tools
并加载其编译器、CMake 和 Ninja，无需重复安装已有工具：

```powershell
./mcp/scripts/build.ps1
$env:PYTHONPATH = "$PWD/mcp/python"
$env:MELONDS_MCP_LIB = "$PWD/build/mcp-direct/melonds_mcp.dll"
./mcp/.venv/Scripts/python.exe -m melonds_mcp
```

Windows 脚本默认 Ninja、`build/mcp-direct/`、解释器（JIT OFF）。DLL 输出在指定的
build 目录；例如本机为 `build/mcp-direct/melonds_mcp.dll`。Python 会自动搜索常用目录，也可用
`MELONDS_MCP_LIB` 指定绝对路径。可选 `MELONDS_MCP_ROM` 在第一次工具调用时加载 ROM。
新服务使用独立 `mcp/.venv`（MCP 1.x），不要与旧 `mcp-server/.venv`（MCP 2.x）混用。

### 接入 MCP 客户端

本工作区可使用 [mcp-config.example.json](mcp-config.example.json) 中的 stdio 配置。
它使用绝对路径，可从任意工作目录启动；移动工程后需要修改路径。
示例不会自动更改任何客户端配置。

```json
{
  "mcpServers": {
    "melonds": {
      "command": "D:/melonDS-MCP/mcp/.venv/Scripts/python.exe",
      "args": ["-m", "melonds_mcp"],
      "env": {
        "PYTHONPATH": "D:/melonDS-MCP/mcp/python",
        "MELONDS_MCP_LIB": "D:/melonDS-MCP/build/mcp-direct/melonds_mcp.dll",
        "GHIDRA_HOME": "D:/melonDS-MCP/build/analysis-tools/ghidra_12.1.3_PUBLIC",
        "JAVA_HOME": "D:/melonDS-MCP/build/analysis-tools/jdk-21.0.12.1+1"
      }
    }
  }
}
```

服务启动后会等待客户端的 MCP 消息，不会弹出模拟器窗口。
客户端可按 `load_rom → advance_frames → screenshot` 工作；调试工作流为
`breakpoint_add → run_until_break → read_registers → step → continue_after_break`。
ROM 使用你有权使用的本地文件，项目不提供商业 ROM 或 BIOS dump。

反编译是可选依赖：已有 Ghidra/JDK 时指向其目录；否则 Windows 可运行
`./mcp/scripts/setup-analysis.ps1`，下载校验后只解压到本工程。基础模拟器无需这些依赖。
详见 [反编译配置、工具与限制](docs/ANALYSIS.md)。

### 新增的检查工作流

- 金手指与改存档：`save_workspace_prepare → load_rom → memory_scan / memory_table_read`
  定位验证，再保护写入、游戏内保存、`backup_export`、独立核心冷启动验收。
  `cheat_generate_ar` 只生成代码、不启用。[工具说明](docs/SAVE_WORKFLOWS.md)与
  [Agent 技能](../.agents/skills/melonds-cheat-save/SKILL.md)记录了本次无限航路全图鉴经验。
- 安全读写：数据使用 `memory_peek → memory_poke`；代码使用 `code_peek → code_patch`。
  写入可带 `expected_hex` 防止过期修改；数据视图跟随 TCM，代码视图忽略 DTCM，详见 API。
- 看图形资源：`gpu_state → gpu_read_vram / gpu_palette / gpu_tiles / gpu_oam`，
  再用 `gpu_tilemap / gpu_sprite` 查看显式物理地址对应的文本地图和单个精灵透明 PNG。
- 真正反编译：`analysis_status → decompile_bytes / decompile_memory`。
  返回 Ghidra 伪 C、CFG、输入 SHA256、CPU/模式和分析器版本；内存分析默认用安全代码视图，
  相同输入可命中进程内结果缓存。

## 已实现与验证范围

- 共68工具：原控制17、调试22、状态9，加GPU 7、安全内存/代码4、分析3、金手指/普通存档6；详见 [API.md](docs/API.md)。
- 所有 MCP 工具共享可重入锁，限制输入范围，拒绝未知参数、隐式类型转换与地址溢出。
- 推进帧默认全渲染；帧计数以 native `NumFrames` 变化为准，暂停/帧中断不凭调用次数计数。
- PNG 截图返回标准 MCP `ImageContent`，另附屏幕尺寸、帧号、断点状态元数据。
- 首次单步先激活请求，再同步 JIT/解释器模式。
- Python 轻量测试当前覆盖工具数、参数约束、JIT 顺序、帧计数、图像输出、串行边界、DLL 查找。
  运行：`mcp/.venv/Scripts/python.exe -m pytest mcp/python/tests -q`。

### 本机实际测试结果（2026-09-05）

下表是原62工具的历史基线；2026-09-14新增工具测试与限制见 [SAVE_WORKFLOWS.md](docs/SAVE_WORKFLOWS.md)。

环境：Windows x64、MSVC 19.44、Ninja、Release，DS 模式、软件渲染、JIT OFF。

| 测试层 | 结果 | 说明 |
| --- | --- | --- |
| Python 工具与分析边界 | 239 项通过 | 工具/解码器、旧 DLL 兼容、参数、缓存、写入前置检查、进程输出上限和受控子进程超时；不替代核心验证 |
| 真实 DLL 基础回归 | 10 项通过 | 双核断点、单步、PC 修改、重叠观察点、暂停、RAM、帧内存档恢复和双屏像素 |
| 真实 GPU 存储回归 | 7 项通过 | 9 个物理 VRAM bank、双引擎调色板/OAM、未映射读取、越界拒绝和状态不变 |
| 真实安全内存回归 | 9 项通过 | CPU 真实写入 ITCM/DTCM、重定位/关闭、镜像、WRAM 分配、BIOS、失败不部分写和状态不变 |
| 真实安全写入/代码修补回归 | 12 项通过 | TCM 数据与指令 backing 区分、两核预取刷新、映射和拒绝路径 |
| 存档边界回归 | 5 项通过 | 错误/截断文件头、长度不符均不改状态；正确快照和旧 MCPR v1 兼容；native 总计 43 项 |
| 基础 MCP stdio 端到端 | 24 次调用通过 | 正式 ClientSession，实际调用 13 种基础工具 |
| 图形 MCP stdio 端到端 | 64 次调用通过 | 5 个 GPU 工具均覆盖，4/8bpp 图块逐像素、4 套调色板、OAM 和双核状态不变 |
| 地图/精灵 MCP stdio 端到端 | 143 次调用通过 | 512×512 screenblock 地图、1D/2D 精灵、翻转、affine double-size，逐像素验证且检查期间状态不变 |
| 内存修补 MCP stdio 端到端 | 128 次成功、11 次预期拒绝 | expected_hex 防过期写、DTCM 两种视图、两核共享镜像预取后的实际执行 |
| 调试 MCP stdio 端到端 | 69 次调用通过 | 两核指令追踪、读/写观察点、断点管理、ITCM ARM/Thumb 反汇编和真实 PC |
| 控制 MCP stdio 端到端 | 110 次成功调用 | ARM7 实际采样按键/触屏输入、轮询、8 类变量观察、存档槽与重置；另测预期拒绝 |
| 真反编译 MCP stdio 端到端 | 19 次调用通过 | Ghidra 12.1.3，ARM9/ARM、ARM7/Thumb、instruction/data 内存视图和缓存；返回值 7→9、hash 更新、模拟状态不变 |

端到端覆盖 ROM 装载、逐帧、双核内存读取、内存修改、存档恢复、ARM7 断点和单步、
寄存器读取，以及标准 MCP 图像返回。自制 ROM 和测试画面由仓库代码生成，未使用商业数据。
截图上屏为红色、下屏为蓝色，尺寸 256×384；软件渲染色值 `(251,0,0)` / `(0,0,251)`
已逐像素验证。测试报告和图像输出在 `build/mcp-e2e/`。

GPU 资源测试报告/图片在 `build/graphics-e2e/`，地图与精灵在 `build/graphics-maps-e2e/`；
反编译报告和伪 C 在 `build/analysis-e2e/`。本机相同小函数首次分析约 20 秒，
重复请求命中缓存约 15 毫秒；这是该测试输入的测量值，不是任意函数的延迟保证。

完整验收只需一条命令（已构建 DLL 和可选分析依赖时）：

```powershell
./mcp/.venv/Scripts/python.exe mcp/tests/verify.py --library build/mcp-direct/melonds_mcp.dll --analysis --ghidra-home build/analysis-tools/ghidra_12.1.3_PUBLIC --java-home build/analysis-tools/jdk-21.0.12.1+1
```

每次生成新的 `build/verification/<时间>/`，包含协议发现的工具 schema、每组日志、图片、
伪 C、逐工具覆盖表与源码/DLL SHA256；验收期间源码或 DLL 改变会报失败。
不带 `--analysis` 可验收基础能力，报告会明确列出未运行的 3 个分析工具。

复现（在仓库根目录）：

```powershell
./mcp/.venv/Scripts/python.exe -m pytest mcp/python/tests -q
./mcp/.venv/Scripts/python.exe mcp/tests/native_smoke.py --library build/mcp-direct/melonds_mcp.dll
./mcp/.venv/Scripts/python.exe mcp/tests/native_graphics.py --library build/mcp-direct/melonds_mcp.dll
./mcp/.venv/Scripts/python.exe mcp/tests/native_memory_peek.py --library build/mcp-direct/melonds_mcp.dll
./mcp/.venv/Scripts/python.exe mcp/tests/native_memory_poke.py --library build/mcp-direct/melonds_mcp.dll
./mcp/.venv/Scripts/python.exe mcp/tests/native_savestate_errors.py --library build/mcp-direct/melonds_mcp.dll
./mcp/.venv/Scripts/python.exe mcp/tests/mcp_e2e.py --library build/mcp-direct/melonds_mcp.dll
./mcp/.venv/Scripts/python.exe mcp/tests/graphics_e2e.py --library build/mcp-direct/melonds_mcp.dll
./mcp/.venv/Scripts/python.exe mcp/tests/graphics_maps_e2e.py --library build/mcp-direct/melonds_mcp.dll
./mcp/.venv/Scripts/python.exe mcp/tests/memory_poke_e2e.py --library build/mcp-direct/melonds_mcp.dll
./mcp/.venv/Scripts/python.exe mcp/tests/debug_workflow_e2e.py --library build/mcp-direct/melonds_mcp.dll
./mcp/.venv/Scripts/python.exe mcp/tests/control_workflow_e2e.py --library build/mcp-direct/melonds_mcp.dll
./mcp/.venv/Scripts/python.exe mcp/tests/analysis_e2e.py --library build/mcp-direct/melonds_mcp.dll --ghidra-home build/analysis-tools/ghidra_12.1.3_PUBLIC --java-home build/analysis-tools/jdk-21.0.12.1+1
```

第一条需要测试依赖 `pytest`；其他命令不使用 fake backend，最后一条需要可选 Ghidra/JDK。
62 个工具均有真实 MCP 工作流调用覆盖；这不代表穷尽了每个参数、每条指令或所有游戏。
Linux/macOS、DSi、JIT、OpenGL 和商业游戏兼容性未在本次环境中验证。

## 当前边界

- 反汇编使用 Capstone；真正反编译使用可选 Ghidra，当前一次分析最多 4096 字节，
  需要已知入口/模式，未提供整个 ROM 的符号、类型或全程序项目分析。
- 已有双屏、VRAM、标准调色板、4/8bpp 图块、OAM、文本 tilemap 和单精灵检查；
  后两者要求显式物理源地址，透明图层不等于完整屏幕合成；尚无扩展调色板、bitmap OBJ 或 3D 纹理查看器。
- 断点可能停在帧中间；截图表示最近完成帧，不强行执行额外帧改变调试状态。
- `memory_poke` 写 TCM-aware 数据视图，不刷新取指预取；`code_patch` 写忽略 DTCM 的指令
  backing，并更新两核中物理匹配的预取。两者最多 4096 字节，拒绝 BIOS/MMIO/VRAM/OAM/卡带/DSi。
  `memory_peek` / `code_peek` 都不是 I-cache 快照；相关真实验证目前仅覆盖默认解释器。
  原 `read_memory` / `write_memory` 保留有可能产生 MMIO 副作用的总线语义，不覆盖 ARM9 TCM overlay。
- PC 写入通过 CPU 跳转路径刷新流水线，保持当前 ARM/Thumb 模式；完整 CPSR 写入明确拒绝。
- `get_pc` / `get_status.pc_arm9/pc_arm7` 是下一指令地址；`read_registers.pc` 保留原始
  流水线 R15，另附 `instruction_address`。周期查询不修改统计基线。
- MCPR v2 存档保存当前调度 CPU，恢复后周期数与帧中间继续点一致；仍可读取 MCPR v1。
  文件/slot 保存与加载均返回真实结果；格式错误文件头在进入组件加载前拒绝。
- 这是独立无头实例，不是附着到已有 melonDS GUI；每个服务进程拥有一个模拟器实例。
- ROM/存档路径属于本地可信工具权限；尚无文件访问白名单，存档应来自可信同版本实例。
- 无头 Platform 的网络、摄像头、麦克风等并非完整宿主设备实现。
- 长执行受帧数上限约束，但执行中不能处理另一个工具请求；需要取消/抢占的工作流仍待专门验证。

后续以 [当前路线图](../docs/mcp/ROADMAP.md) 为准。旧 `mcp-server/` 与 `src/mcp/`
保留为 GDB/IPC 研究分支的本地代码，不是上述默认启动路径。
