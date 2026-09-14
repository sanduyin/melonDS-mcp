# 金手指定位与普通存档工作流

当前增加6个工具，总计68个。它们复用现有安全数据读写和电池存档 C API，无需增加 DLL 导出或重新编译核心。它们不猜测地址、不自动启用代码、不自动推进游戏，也不覆盖已有文件。

## 工具与边界

| 工具 | 用途与关键参数 |
| --- | --- |
| `save_workspace_prepare` | `rom_path, save_path, workspace_dir`：复制到全新目录，生成 `game.nds`、`game.sav`、`original.sav` 与 SHA256 manifest；返回游戏代码和修订号，不自动加载 |
| `backup_export` | `path`：导出已加载卡带的普通 `.sav`；文件必须不存在，返回长度和 SHA256，不等于游戏保存 |
| `memory_scan` | `address, span, pattern_hex, alignment=1, cpu=0`：精确字节搜索；最多4 MiB、模式1..16字节，最多返回256个地址并标明截断 |
| `memory_table_read` | `address, stride, count, width=1, cpu=0`：读取带间隔字段，返回值分布与整个跨度 SHA256 |
| `memory_table_patch` | 上述参数加 `value, replace_values, expected_sha256`：只替换列出的旧值，保留其他值和邻近字段，逐块保护与读回验证 |
| `cheat_generate_ar` | `address, value, width=4, count=1, stride=4, activation="select"`：生成直接写或循环 AR 代码，仅返回文本，不安装、不启用 |

数组跨度上限64 KiB、条目上限4096、宽度1/2/4字节。内存研究工具限制在普通 DS 主 RAM 的规范地址 `0x02000000..0x023FFFFF`；不支持 DSi、MMIO、VRAM、WRAM 或别名地址。底层工具仍可用于其他已支持安全区域。

所有新工具由现有 `ToolBoundary` 串行化并严格验证参数。表写入先核对整段哈希；多块写入不是全局原子事务，中途失败会说明已验证块数，失败块本身也可能改变。应从隔离备份恢复后重新定位，而不是在可能部分修改的状态上盲目重试。单块保护不能证明地址语义正确。

工作目录/导出路径使用本机工具权限，并不是路径沙箱。准备工具拒绝已存在目录，导出使用独占创建拒绝已存在文件；复制过程中源文件变化会拒绝使用结果。I/O错误可能留下独立的部分输出供检查，不会据此删除用户源文件。源路径与 manifest 留在本机，不要上传个人工作目录。

## 推荐流程

1. 选定用户的 ROM 与普通存档，以 `save_workspace_prepare` 建立隔离副本；`load_rom` 返回的副本路径。
2. 记录 ROM SHA256、游戏代码/修订、运行阶段。金钱用小端值扫描并通过游戏内增减缩小候选；图鉴读取数组分布，对照屏幕解锁比例。地区名称不是地址正确性的证据。
3. 单值 `memory_poke(..., expected_hex=...)`；数组 `memory_table_read → memory_table_patch`，显式保留更强/未知值。对照画面，并核对未请求修改的内容。
4. 若交付 AR 代码，再确认 ARM7 总线视图。生成器的16/32位地址和步长要求对齐。AR编码依据本仓库 `src/AREngine.cpp`：写入类型0/1/2，`C0` 循环参数为次数减1，`DC` 加步长，`D2` 循环/清理。Select条件为 `94000130 FFFB0000`，是按住时重复执行，不是一次性。持续写入/覆盖强标记的风险必须说明。
5. 若交付普通存档，在用户选定的栏位通过游戏内保存，等待完成，再 `backup_export` 到新路径。导出不会把任意 RAM 自动序列化为游戏格式，也不会替游戏计算所有保存字段。
6. 结束核心，在新进程里用导出 `.sav` 和同一个 ROM 冷启动；不开金手指、不施加补丁、不加载即时存档。检查图鉴和重新读入的剧情，才能报告持久成功。

MCP 每个服务进程有自己的核心，不会附着到现有 GUI。加载即时存档可能回写邻近普通 `.sav`；不要通过加载旧即时存档来验证新存档。`reset_emulation` 也不替代独立进程冷启动。

## 可复用 Agent 技能

仓库技能入口：[melonds-cheat-save](../../.agents/skills/melonds-cheat-save/SKILL.md)。支持仓库技能发现的客户端可自动使用；也可按客户端约定安装整个技能目录，并将技能中的项目说明链接相应指向此仓库。本项目不自动修改全局客户端配置。

[无限航路特定汉化构建的实测案例](../../.agents/skills/melonds-cheat-save/references/infinite-space.md)包含版本哈希、四张图鉴表及持久性验证结论，不分发游戏或玩家数据。这些地址不是所有日版/汉化版通用配方。

## 自动验证

```powershell
./mcp/.venv/Scripts/python.exe -m pytest mcp/python/tests -q
./mcp/.venv/Scripts/python.exe mcp/tests/save_workflows_e2e.py --library build/mcp-direct/melonds_mcp.dll
```

本次开发中 Python 测试273项通过（含新工具34项），真实 DLL + MCP stdio 工作流14次成功调用与3次预期拒绝。新测试使用完全自制、无商业素材的双核 EEPROM 测试卡带，验证多块表写入、保留强标记/相邻字段、哈希过期拒绝、普通存档导出/独立进程重读，以及“改RAM不等于已经存盘”。生成的 AR 文本经过格式/语义单元测试，尚未在该端到端测试中交给 AR 引擎执行；`runtime_verified` 保持 false。真实游戏四类图鉴100%的证据是单独的上述手工案例，不冒充可再分发的自动夹具。

`mcp/tests/verify.py` 已纳入此新工作流。2026-09-14整体回归通过：273项Python测试、43项native测试、566次MCP调用，覆盖全部65个非Ghidra工具；源码与DLL在验证期间保持不变。可选Ghidra的3个工具本次未运行，不计入这65项。原2026-09-05的62工具全量验收属于历史基线；当前报告生成在 `build/verification/<时间>/result.json`，测试产物不提交。
