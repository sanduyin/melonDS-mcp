# melonDS-MCP 当前路线图

状态：可用实现已完成本机验收，发布准备完成。更新日期：2026-09-05。

主线已改为 [sanduyin/melonDS-mcp](https://github.com/sanduyin/melonDS-mcp)
的 `3b39290543904bbafe62ec5c4f208b69172212ec` 直连实现，并合入本机验证过的修复。
上游基础为 `906e9ebb27da8c6a715cd7abab4abfe8a8d29427`。
完整构建和使用方法见 [mcp/README.md](../../mcp/README.md)。

## 当前可用路径

`Agent → MCP stdio → Python/ctypes → melonDS shared core`

每个服务进程拥有一个无头实例；工具整体串行执行，模拟时间只在执行类工具调用中推进。
不需要 Qt、GUI attach、GDB server 或新建本地 IPC 协议。

| 范围 | 当前状态 | 证据或限制 |
| --- | --- | --- |
| Windows 无头构建 | 已实测 | MSVC 19.44 / Ninja / Release，DS、软件渲染、JIT OFF |
| MCP 工具 | 62/62 个有真实 MCP 调用覆盖 | 原 48 + GPU 7 + 安全内存 4 + 分析 3；完整结果见 [验收报告](VERIFICATION.md) |
| Python 工具与分析边界 | 239 项测试通过 | 类型、范围、未知参数、串行锁、图形解码、保护写入、缓存与依赖/进程边界 |
| 核心调试 | 10 项真实 DLL 测试通过 | ARM9/ARM7 断点、单步、PC 写入、暂停、RAM、重叠观察点、帧中断恢复 |
| 完整 Agent 调用链 | 24 次调用通过，覆盖 13 个工具 | 正式 MCP stdio 客户端，自制双核 ROM、内存、断点、存档、截图 |
| GPU 资源检查 | 7 项 native 回归 + 64 次真实 MCP 调用通过 | 9 个 VRAM bank、标准调色板、4/8bpp tiles、OAM，图像逐像素与状态不变 |
| Tilemap / 精灵图片 | 143 次真实 MCP 调用通过 | 512×512 text map 分块，1D/2D 精灵、翻转、透明像素及仿射双倍边界逐像素检查 |
| 无副作用内存 | 9 项 native 回归通过 | RAM/WRAM/ITCM/DTCM/BIOS data backing；MMIO 等不支持区拒绝 |
| 安全写入 / 指令补丁 | 12 项 native 回归通过 | TCM-aware data poke，双核 ARM/Thumb 物理镜像预取一致性；拒绝范围不部分写入 |
| MCP 内存修补 | 128 成功调用 + 11 预期拒绝 | DTCM data/code 分离、expected_hex、两核共享镜像补丁后真实执行 |
| MCP 控制/状态 | 110 成功调用 + 10 预期拒绝 | ARM7 实际采样按键/触屏、轮询、8 种 watch、slot 存档恢复与重置 |
| MCP 调试 | 69 次调用通过 | 两核 trace、读/写观察点、断点管理、ITCM 反汇编、下一指令 PC |
| 存档错误与兼容性 | 5 项 native 回归通过 | 错误文件头不改状态、正常 roundtrip、MCPR v1 兼容；MCPR v2 保存调度 CPU |
| 真正反编译 | 实际 Ghidra 12.1.3 调用通过 | ARM9 ARM、ARM7 Thumb、实时 RAM 修改后伪 C 与哈希更新；成功结果有界缓存 |
| 反汇编 | Capstone 工具已接入 | 保持独立名称，不与真正反编译混淆 |
| 旧 GDB/IPC 工作 | 保留，不是默认路径 | `mcp-server/`、`src/mcp/`；旧 Python 测试 45 项通过，不代表 GUI E2E |

回归入口：

- `mcp/python/tests/test_facade.py`：轻量 fake native 单元测试。
- `mcp/tests/native_smoke.py`：真实 DLL + 仓库生成的 ARM9/ARM7 测试 ROM。
- `mcp/tests/mcp_e2e.py`：真实 MCP 子进程；输出 `build/mcp-e2e/result.json` 与双屏 PNG。
- `mcp/tests/native_graphics.py` / `native_memory_peek.py`：新增原生存储与数据映射验证。
- `mcp/tests/native_memory_poke.py`：数据写入与代码补丁的真实 CPU 执行、镜像、TCM、拒绝原子性。
- `mcp/tests/graphics_e2e.py` / `analysis_e2e.py`：新工具的真实 MCP 图像和 Ghidra 反编译链路。
- `mcp/tests/graphics_maps_e2e.py`：地图与单精灵 RGBA 图片逐像素回归。
- `mcp/tests/control_workflow_e2e.py` / `debug_workflow_e2e.py` / `memory_poke_e2e.py`：完整 Agent 操作链。
- `mcp/tests/verify.py`：一次执行所有验收，并输出真实工具覆盖、源码/DLL 指纹和独立日志。

## 后续可扩展方向

1. **图形资源扩展**：物理 bank、调色板、OAM、tiles、text tilemap、单精灵已完成真实验证；
   后续可增加自动图层映射/完整合成和 3D 资源，继续用人工 pattern 与真实运行证据验证。
2. **分析扩展与性能**：真正反编译、instruction/data backing 选择和有输入/版本指纹的重复
   分析缓存已接入；后续扩展较大函数/外部数据映射。伪 C 不是原始源码或语义证明。
3. **内存修改语义**：TCM-aware `memory_poke` 与 `code_patch` 已覆盖解释器 ARM/Thumb
   预取一致性。`expected_hex` 防止过期覆盖；JIT 缓存失效仅有源码审核，未做 JIT 运行覆盖。
4. **调试和存档边界**：扩展 ARM/Thumb、各宽度读写观察点和 banked register 测试；
   保持未实现的 CPSR 写入拒绝；加强坏存档验证和失败恢复，补充输入状态重放。
5. **运行控制与交付**：补齐长操作取消、路径权限和可审计错误；实际验证更多工具及 ROM，
   再扩大到 Linux/macOS、DSi、JIT/OpenGL。GUI attach 是独立扩展，不阻塞无头工作流。

## 已知边界

- 原内存工具仍走总线，MMIO 可能有副作用且绕过 TCM；数据优先用 `memory_peek`/`memory_poke`，
  代码用 `code_peek`/`code_patch`。ARM9 instruction backing 忽略 DTCM，保留 ITCM；两种 peek
  都不是 I-cache/预取快照。普通 data poke 不刷新已预取指令。
- 反编译默认 instruction backing，显式 `view="data"` 可读数据视图。缓存进程内最多 8 项/
  8 MiB JSON，只复用成功结果；`MELONDS_MCP_ANALYSIS_CACHE=0` 禁用，不依靠过期时间判断代码新旧。
- Tilemap/精灵工具要求明确物理 bank/offset，只输出资源 RGBA 图，不声称是完整屏幕合成。
- 断点能停在帧中间；截图返回最近完成帧，不为截图偷偷推进模拟。
- `resume` 只恢复可推进状态，不在工具调用之间启动后台实时执行。
- CPSR 完整写入明确拒绝；PC 修改保持当前 ISA 并刷新流水线。
- 无头网络、摄像头、麦克风等平台能力尚不完整。
- 不应向不可信调用者暴露 ROM/存档文件工具；尚无文件白名单或完整恶意存档防护。
- 已验证所有 62 个工具的工作流行为；尚未穷尽参数/指令组合、商业游戏兼容性、跨平台、DSi 或 JIT/OpenGL。

## 完成判断

目标不能仅以工具能注册、DLL 能加载或 mock 测试通过作为完成。
至少应能由 Agent 完成加载、输入、运行、暂停、可靠双核调试、内存修改、
存档恢复、图形资源检查及真正反编译的可复现工作流，并为每项能力提供真实测试证据。
上述操作链已通过真实 MCP 子进程和本机核心验收；图形逐像素、代码修补后真实 CPU 执行、
反编译及输入实际生效都有独立证据。[验收记录](VERIFICATION.md) 汇总范围和复现命令。
上面的扩展方向不属于当前已验证能力的承诺；发布保留这些限制，供后续迭代选择。

[ARCHITECTURE.md](ARCHITECTURE.md) 和 [PROTOCOL.md](PROTOCOL.md) 是旧 GDB/IPC 设计参考，
不再作为当前实现的协议说明或必须先完成的工程门槛。
