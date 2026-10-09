# melonDS MCP 验收记录

2026-09-05，在 Windows x64 / MSVC 19.44 / Ninja Release 上完成完整验收。
使用 DS 解释器与软件渲染，真实 MCP stdio 客户端、真实 melonDS DLL；测试 ROM
全部由仓库自制 ARM9/ARM7 代码生成。可选分析器为 Ghidra 12.1.3 / JDK 21.0.12.1。

| 用户需求 | 实现与实际验证 |
| --- | --- |
| Agent 即时操作 | MCP 直接调用核心；加载、按键/触屏/翻盖、逐帧、暂停/恢复、条件等待、重置、存档槽。ARM7 测试程序在输入保持期间实际采样 KEYINPUT/EXTKEYIN |
| 读取和修改内存 | 双核 RAM/WRAM、ARM9 ITCM/DTCM 安全视图，4096 字节上限；expected_hex 过期写保护、范围整体拒绝、读回和 SHA256 |
| 调试 | 双核寄存器、断点、单步、读/写观察点、原始指令追踪；get_pc 与断点的下一指令地址一致；代码补丁刷新两核物理共享镜像预取，ARM/Thumb 实际执行新值 |
| 反汇编/反编译 | Capstone ARM/Thumb 指令视图；Ghidra 返回伪 C 和 CFG。实时内存常数 7→9 后伪 C/hash 更新；代码/数据视图选择与缓存命中正确 |
| 查看图形 | 双屏 PNG、VRAM、调色板、OAM、4/8bpp 图块、文本地图与单精灵 RGBA PNG；分块、翻转、透明、1D/2D、仿射双倍边界逐像素核对 |
| 可复现和接入 | 构建脚本、标准 MCP 客户端配置、统一验收入口；全部 62 个发布工具有真实工作流调用覆盖 |

完整运行结果：239 项 Python 测试、43 项原生 DLL 测试、7 组真实 MCP 工作流全部通过。
各工作流报告的 `calls` 合计 568，包含内存修补报告中的 11 次预期拒绝；其余单独记录的
负向检查不计入这个合计。工具覆盖是 62/62，不是穷尽参数、CPU 指令或游戏组合的声明。

| 工作流 | 报告 calls | 核心证据 |
| --- | ---: | --- |
| mcp_e2e | 24 | ROM、运行、断点、单步、内存、存档、256×384 双屏像素 |
| graphics_e2e | 64 | 9 个 VRAM bank、4 套调色板、tiles、OAM |
| graphics_maps_e2e | 143 | 512×512 地图及精灵 RGBA 逐像素 |
| memory_poke_e2e | 139 | 128 成功 + 11 预期拒绝，TCM 与双核预取补丁 |
| debug_workflow_e2e | 69 | 两核追踪/观察点、断点表与 ITCM 反汇编 |
| control_workflow_e2e | 110 | 客体输入采样、状态/轮询、8 类 watch、slot/reset；另有 10 次预期拒绝 |
| analysis_e2e | 19 | 真实 Ghidra ARM/Thumb、实时内存视图、缓存与输入失效 |

本次同一小函数首次反编译 23.172 秒，缓存命中 0.031 秒；此前本机重复测量约
19.9 秒/0.015 秒。耗时随输入与宿主负载变化，首次分析需要启动 Java。

验收期间源码与 DLL 没有变化。源码清单组合 SHA256：
`402eb2dfb4463b4acdcf0caabe54c8d5ca96d436e1a037688035ed1971ad648b`。
本次 DLL SHA256：`5b732f4df983339b3290a9432ac8f510e709768b4c7bcb93d62a27587e55bd7b`。
这些是此次构建证据；不同工具链重新编译不要求产生相同 DLL 哈希。

原始本机报告目录为 `build/verification/20260905T123218190746Z/`，包括工具 schema、
各组日志、图片、伪 C、逐工具覆盖及逐文件 SHA256。生成物不提交 Git，按以下命令重新生成：

```powershell
./mcp/scripts/build.ps1
./mcp/scripts/setup-analysis.ps1
./mcp/.venv/Scripts/python.exe mcp/tests/verify.py --library build/mcp-direct/melonds_mcp.dll --analysis --ghidra-home build/analysis-tools/ghidra_12.1.3_PUBLIC --java-home build/analysis-tools/jdk-21.0.12.1+1
```

已有分析依赖可跳过下载并指定目录。MCP 客户端配置见 [mcp-config.example.json](../../mcp/mcp-config.example.json)，
工具说明见 [API](../../mcp/docs/API.md)。全部基础工具无需 Ghidra、商业 ROM 或 BIOS dump。

当前能力边界：独立无头实例；未实测 GUI attach、Linux/macOS、DSi、JIT/OpenGL；图形源地址
需显式指定，尚无完整图层合成或 3D 纹理查看器；反编译每次最多 4096 字节、需明确入口；
完整 CPSR 写入拒绝；存档使用可信同版本数据，错误文件头检查不等于完整恶意文件审计。
