# 真正反编译：Ghidra 后端

`decompile_bytes` / `decompile_memory` 使用 Ghidra 的 `DecompInterface` 生成伪 C，
与 Capstone `disassemble` 工具分开。分析器独立运行，不执行输入的 ARM 程序，也不运行模拟帧。
输入代码上限 4096 字节，需要调用者明确函数入口和初始 ARM/Thumb 模式。

## 准备依赖

基础模拟、调试和图形工具不依赖 Ghidra。反编译需要官方 Ghidra 发行包和 64 位 JDK。
当前验证版本为 Ghidra 12.1.3 / Temurin JDK 21.0.12.1；官方版本和安装要求见
[Ghidra 发布页](https://github.com/NationalSecurityAgency/ghidra/releases/tag/Ghidra_12.1.3_build)
与 [安装说明](https://github.com/NationalSecurityAgency/ghidra/blob/Ghidra_12.1.3_build/GhidraDocs/GettingStarted.md)。

Windows 可运行可选的本地准备脚本：

```powershell
./mcp/scripts/setup-analysis.ps1
```

脚本下载固定官方 ZIP 并校验 SHA256，只解压到 `build/analysis-tools/`，不安装系统服务、
不改全局 PATH、不覆盖已有 Ghidra。下载共约 775 MB，解压还需要额外磁盘空间。
也可以直接使用你已有的 Ghidra/JDK，无需运行脚本。

在 MCP 服务的环境配置中增加（按实际安装位置调整）：

```json
{
  "GHIDRA_HOME": "D:/melonDS-MCP/build/analysis-tools/ghidra_12.1.3_PUBLIC",
  "JAVA_HOME": "D:/melonDS-MCP/build/analysis-tools/jdk-21.0.12.1+1"
}
```

`analysis_status` 检查配置和必要文件，返回 `available`、版本、支持语言、输入限制和 `missing`。
它不启动分析器；真正执行是否成功由每次反编译结果证明。未配置依赖时工具明确报不可用。

## 工具

| 工具 | 参数 | 用途 |
| --- | --- | --- |
| `analysis_status` | 无 | 查看可选后端及缺失依赖 |
| `decompile_bytes` | `hex_data, base_address, entry_address, thumb=false, timeout_seconds=60, cpu=0` | 对明确提供的机器码做静态反编译 |
| `decompile_memory` | `address, length=256, cpu=0, thumb=false, timeout_seconds=60, view="instruction"` | 复制指定安全 backing 视图，以首地址作为函数入口；可选 `view="data"` |

- `cpu=0` 使用 `ARM:LE:32:v5t`，`cpu=1` 使用 `ARM:LE:32:v4t`；均为 little-endian。
- ARM 入口按 4 字节对齐，Thumb 按 2 字节对齐；用 `thumb=true`，不要把入口最低位置 1。
- 入口必须在提供字节范围内，且至少包含一条完整指令。超时范围为 1–120 秒。
- `decompile_memory` 默认 `view="instruction"`，使用与 `code_peek` 相同的指令 backing：
  RAM、WRAM、ARM9 ITCM 和受支持的 BIOS，**不叠加 DTCM**。显式 `view="data"` 使用
  `memory_peek` 数据映射，可观察 DTCM；不读取 MMIO、卡带设备、GPU 或未知地址。
  两者均不是 CPU I-cache 或已预取指令快照；旧 DLL 缺少对应 API 时明确报错，不偷偷降级。
- 模拟器工具锁覆盖取快照和分析过程，因此一次分析中模拟状态不会被另一个工具改变。

修改被分析的代码时，用 `code_peek → code_patch(expected_hex=原值)`；修改 DTCM 等数据时用
`memory_peek → memory_poke`。后者不刷新预取，不能保证修改立即影响下一条已预取指令。
写入范围及限制见 [API 的安全内存章节](API.md#五安全内存与真正反编译)；当前验证使用解释器，未验证 JIT。

例如，以下机器码是仓库自制的 ARM `mov r0, #7; bx lr`：

```json
{
  "hex_data": "0700a0e31eff2fe1",
  "base_address": 33554432,
  "entry_address": 33554432,
  "cpu": 0,
  "thumb": false
}
```

其真实 Ghidra 输出包括：

```c
undefined4 mcp_entry(void)
{
  return 7;
}
```

## 返回结果与边界

结果包含 `kind=decompilation`、`c_code`、截断标记、函数入口/签名/指令数量/地址范围、
CFG 节点和边、分析器及原生 decompiler 版本、输入 SHA256、CPU/ISA/模式，以及诊断。
地址输出为明确的十六进制字符串；MCP 输入地址为 JSON 整数。
实时内存分析另附 `snapshot`，包括帧号、地址、长度、`view`、访问语义和同一输入哈希。
`cache` 标明是否命中、key 与原始分析耗时；`elapsed_seconds` 是本次调用耗时。

缓存未命中时使用独立临时工程和 settings/cache/tmp。直接启动 Java，不经过 shell 或批处理参数
展开；日志不进入 MCP stdout，日志/结果大小受限。超时会终止该调用拥有的进程树并清理临时工程。

伪 C 是启发式分析结果，不是原始源代码或语义正确性的证明。只有提供的字节被映射，外部函数、
全局数据、符号和类型信息不会凭空补出；混合代码/数据、跳转到范围外和错误模式可能导致警告或失败。
首次分析仍需要启动 Java 和编译分析脚本，不适合作为逐帧实时工具。

## 结果缓存与本机测量

默认启用进程内 LRU 成功结果缓存，最多 8 项及 8 MiB 序列化 JSON，不落盘持久化。
key 包含机器码 SHA256、CPU/模式、base/entry、脚本内容和分析器/JDK 安装指纹；输入字节
或相关配置改变会重新分析。安装指纹使用路径、文件大小和时间戳，不等于重新校验整套依赖的内容哈希。
失败和超时不缓存。环境变量 `MELONDS_MCP_ANALYSIS_CACHE=0` 可禁用缓存；重启服务也会清空。

`decompile_memory` 每次仍先读取当前视图，并为本次调用生成新 `snapshot`；不会复用缓存中的旧帧号。
相同机器码和分析条件可在字节输入/实时内存输入之间共享结果；更改内存后 hash 不同，不会命中旧代码。
`analysis_status` 返回缓存启用状态和容量统计。

真实 MCP 测试入口为 `mcp/tests/analysis_e2e.py`，结果输出到 `build/analysis-e2e/`。
本机报告记录 19 次工具调用：ARM9/ARM 返回常数、ARM7/Thumb 两参数相加、重复请求缓存命中、
两种实时模拟器内存视图，以及修改内存后的返回值 7→9 和 SHA256 改变。
其中相同小函数首次分析约 20 秒，重复请求约 15 毫秒，精确测量见每次 `result.json`。
这些是特定环境/输入的观测，不是缓存未命中或任意函数的性能保证。
测试同时验证两核寄存器、帧号/VCOUNT 在分析期间不变，MMIO peek 与不对齐入口被拒绝。
