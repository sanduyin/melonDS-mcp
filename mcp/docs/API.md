# melonDS-mcp API 文档

本接口从 sanduyin/melonDS-mcp 的 commit `3b39290543904bbafe62ec5c4f208b69172212ec`（GPLv3）导入并修正，保留原48个工具，另有GPU 7、安全内存/代码4、真正反编译分析3、金手指/普通存档6，共68个工具。新增的 `save_workspace_prepare`、`backup_export`、`memory_scan`、`memory_table_read`、`memory_table_patch`、`cheat_generate_ar` 参数与限制见 [普通存档工作流](SAVE_WORKFLOWS.md)。接口覆盖与实现不等于跨平台真实 ROM 验收；当前验证范围见 [README](../README.md)。

## 架构总览

```
AI agent (Claude / 其他 MCP 客户端)
        │  MCP (stdio, JSON-RPC)
        ▼
melonds_mcp Python 服务器 (FastMCP)
   ├── tools_control  操控系统
   ├── tools_debug    调试工具集
   ├── tools_status   状态查看组件
   └── tools_graphics 原始 GPU 资源检查
        │  ctypes
        ▼
melonds_mcp.dll / libmelonds_mcp.dylib / .so  (C shim 层)
        │  C++ 直接调用
        ▼
melonDS 核心 (含 MCPDebug 调试钩子)
```

- **通信方式**：MCP over stdio（标准输入输出），与 Claude Desktop / Trae / Cursor 等 MCP 客户端兼容。
- **执行模型**：模拟器与 MCP 服务器同进程同步执行。全部工具通过共享可重入锁串行访问；只有推进类调用执行模拟，恢复状态本身不启动后台循环。
- **调试实现**：指令级断点/单步/追踪通过核心新增的 `src/MCPDebug` 钩子实现（仅在解释器模式下生效，激活调试功能时自动关闭 JIT，全部停用后自动恢复）。
- **分析路径**：`tools_analysis` 在独立 Java/Ghidra 子进程中分析字节快照，仍通过同一个 MCP 服务返回结果；不依赖 GDB 或 GUI，也不执行输入代码。

## 环境变量

| 变量 | 说明 | 默认 |
|---|---|---|
| `MELONDS_MCP_LIB` | libmelonds_mcp 库路径 | 仓库 `build/mcp/`、`build/mcp/Release/` 等目录自动查找 |
| `MELONDS_MCP_ROM` | 服务器启动时自动加载的 ROM 路径 | 无 |
| `GHIDRA_HOME` | 可选 Ghidra 发行包目录，用于真正反编译 | 无；分析工具明确报告不可用 |
| `JAVA_HOME` | 可选 64 位 JDK 目录，Ghidra 12.1.3 要求 JDK 21+ | 无 |
| `MELONDS_MCP_ANALYSIS_CACHE` | 设为 `0` 禁用进程内反编译结果缓存 | 启用；最多 8 项/8 MiB JSON |

---

## 一、操控系统（tools_control）

模拟器控制与输入注入。

| 工具 | 参数 | 说明 |
|---|---|---|
| `load_rom` | `path` | 加载 NDS ROM 并启动（自动读取同目录 .sav） |
| `pause_emulation` | — | 暂停模拟 |
| `resume_emulation` | — | 恢复模拟 |
| `reset_emulation` | — | 重置（断点保留） |
| `is_running` | — | 查询运行状态 |
| `press_buttons` | `buttons[], frames=1` | 按下按键保持 N 帧后释放 |
| `set_buttons` | `buttons[]` | 设置持续按住的按键（空列表=全释放） |
| `get_buttons` | — | 查询当前按键 |
| `tap_screen` | `x, y, frames=1` | 点击下屏坐标（0-255 × 0-191） |
| `set_touch` | `x, y` | 持续按住触摸点 |
| `release_touch` | — | 释放触摸 |
| `set_lid` | `closed` | 开合翻盖（休眠） |
| `advance_frames` | `frames=1` | 推进 N 帧并渲染，实际完成帧数以 native 计数为准 |
| `advance_frames_until` | `address, expected, size=4, cpu=0, max_frames=3600` | 推进直到内存值等于期望（游戏状态轮询） |
| `run_until_break` | `max_frames=3600` | 持续运行直到断点命中 |
| `savestate_save` | `path` 或 `slot` | 保存即时存档 |
| `savestate_load` | `path` 或 `slot` | 加载即时存档 |

**按键名**：`a b x y l r start select up down left right`

### 典型流程

```
load_rom("/path/game.nds")
advance_frames(120)            # 跳过开场 2 秒
press_buttons(["a"], 5)        # 按 A 确认
tap_screen(128, 96, 10)        # 点击屏幕中央
screenshot()                   # 查看结果
```

---

## 二、调试工具集（tools_debug）

### 内存读写

| 工具 | 参数 | 说明 |
|---|---|---|
| `read_memory` | `address, length=4, cpu=0, fmt="hex"` | 读取内存；fmt: hex/u8/u16/u32/s8/s16/s32/float/ascii |
| `write_memory` | `address, value, size=4, cpu=0` | 写入单个值（1/2/4 字节） |
| `write_memory_bytes` | `address, hex_data, cpu=0` | 按十六进制串批量写入 |

常用内存区域（ARM9 视图）：主 RAM `0x02000000`-`0x023FFFFF`（4MB）、共享 WRAM `0x03700000`、ARM9 ITCM `0x00000000`、VRAM `0x06000000`。

### 寄存器监控

| 工具 | 参数 | 说明 |
|---|---|---|
| `read_registers` | `cpu=0` | 完整寄存器：R0-R15、CPSR（含 N/Z/C/V/T 位与 CPU 模式解码）、FIQ/SVC/ABT/IRQ/UND 分组寄存器 |
| `write_register` | `cpu, name, value` | 写入 r0-r12/sp/lr/pc；CPSR 完整写入明确拒绝 |
| `get_pc` | `cpu=0` | 下一待执行指令地址，与断点/trace 地址一致 |
| `disassemble` | `address, count=10, cpu=0, thumb=None` | 安全指令 backing 反汇编（含 ITCM、排除 DTCM；需 capstone；thumb 缺省按 CPSR T 位判断） |

`read_registers.pc` 是原始流水线 R15，另附 `instruction_address`。ARM 时后者为 R15−4，
Thumb 时为 R15−2；`get_status.pc_arm9/pc_arm7` 也返回该指令地址。反汇编要求按 ISA 对齐，
每条最多读取 4 字节，单次最多 1024 条/4096 字节；禁止 MMIO 总线 fallback。

### 断点管理

| 工具 | 参数 | 说明 |
|---|---|---|
| `breakpoint_add` | `cpu, address` | 添加 PC 断点，返回 id |
| `breakpoint_remove` | `bp_id` | 按 ID 删除 |
| `breakpoint_list` | `cpu=-1` | 列出（可按 CPU 过滤） |
| `breakpoint_clear` | `cpu=-1` | 清除 |

### 观察点（数据断点）

| 工具 | 参数 | 说明 |
|---|---|---|
| `watchpoint_add` | `cpu, address, size=4, kind="rw"` | 监视内存范围，kind: r/w/rw |
| `watchpoint_remove` / `watchpoint_list` / `watchpoint_clear` | — | 管理观察点 |
| `watchpoint_events` | — | 取出命中事件（CPU/地址/触发 PC/方向/写入值） |

### 指令追踪与单步

| 工具 | 参数 | 说明 |
|---|---|---|
| `trace_start` | `cpu_mask=1, address_start=0, address_end=0xFFFFFFFF` | 开始追踪（环形缓冲 65536 条） |
| `trace_stop` | — | 停止追踪 |
| `trace_get` | `max_entries=1000` | 取出并清空缓冲（CPU/PC/指令码/CPSR） |
| `step` | `count=1, cpu=0` | 单步执行 N 条指令后暂停 |
| `get_break_info` | — | 查询命中状态（breakpoint/watchpoint/step） |
| `continue_after_break` | — | 确认命中并继续 |

### 断点调试典型流程

```
breakpoint_add(cpu=0, address=0x02000120)   # ARM9 ARM 指令断点
run_until_break()                            # 运行到命中
get_break_info()                             # 确认命中位置
read_registers(cpu=0)                        # 检查寄存器
disassemble(address=pc, count=8, cpu=0)      # 查看附近指令
read_memory(address=0x02100000, length=16)   # 检查内存
step(count=1)                                # 单步
continue_after_break()                       # 继续
```

---

## 三、状态查看组件（tools_status）

| 工具 | 参数 | 说明 |
|---|---|---|
| `get_status` | — | 完整状态：运行标志、帧/延迟帧计数、双 CPU PC、帧率、模拟速度、JIT、调试状态、ROM 信息 |
| `get_performance` | — | 实测 FPS（1 秒滚动窗口）、模拟速度倍率、宿主进程 CPU/内存占用（psutil） |
| `screenshot` | `screen="both", format="png"` | 返回标准 MCP PNG ImageContent 和帧号/尺寸 metadata（top/bottom/both）；format=rgb_hex 返回原始像素十六进制 |
| `get_rom_info` | — | ROM 头：标题、游戏代码、厂商、ARM9/ARM7 装载地址与入口 |
| `watch_add` | `label, address, wtype="u32", cpu=0, length=16` | 添加游戏变量观察项 |
| `watch_list` | `read_values=True` | 列出观察项并读取当前值（游戏状态可视化） |
| `watch_remove` / `watch_clear` | — | 管理观察项 |
| `get_system_info` | — | 系统能力信息（模拟周期数、JIT 状态、可选依赖可用性） |

**观察类型**：`u8 u16 u32 s8 s16 s32 float ascii`

### 游戏状态监视示例

```
watch_add(label="player_hp", address=0x02123456, wtype="u16")
watch_add(label="coins", address=0x02123458, wtype="u32")
watch_add(label="player_name", address=0x02124000, wtype="ascii", length=12)
advance_frames(600)
watch_list()          # => player_hp: 87, coins: 1234, player_name: "ASH"
```

---

## 四、GPU 资源检查（tools_graphics）

这些工具复制 GPU 的物理存储，不经过 MMIO、不推进模拟。每个结果附带
`frame_number`、`vcount` 和 `access=direct_gpu_copy`，图像还附源数据、像素和 PNG 的 SHA256。
一个工具中的状态、数据与图片在同一串行锁下取得。

| 工具 | 参数 | 结果 |
| --- | --- | --- |
| `gpu_state` | 无 | 双引擎 DISPCNT、POWCNT1、帧/扫描线、9 个 VRAMCNT 与 bank 大小 |
| `gpu_read_vram` | `bank="A".."I", offset=0, length=256` | 最多 4096 字节的物理 VRAM hex；即使 bank 未映射也可读，越界整体拒绝 |
| `gpu_palette` | `engine="A"/"B", kind="bg"/"obj"` | 256 色标准调色板的 RGB555/RGB8 条目与 192×192 色块图 |
| `gpu_tiles` | `bank, offset=0, bpp=4/8, palette_engine="A", palette_kind="bg", palette_index=0, tile_count=64, columns=8` | 连续 8×8 图块 PNG，最多 256 块；4bpp 每字节低半字节先，8bpp 使用完整 256 色表 |
| `gpu_oam` | `engine="A"/"B"` | 128 个 OBJ 条目：原始属性、位置、尺寸、翻转、优先级、tile index、调色板和共享 affine 矩阵 |
| `gpu_tilemap` | `map_bank, map_offset, tile_bank, tile_offset, bpp=4, palette_engine="A", map_width=32, map_height=32` | 宽高各 32/64 tiles 的 DS text BG 地图；32×32 screenblock 顺序、10-bit tile index、H/V flip、4bpp palette bank，最大 512×512 RGBA |
| `gpu_sprite` | `engine, index, bank, offset=0` | OAM `index=0..127` 的单精灵；DISPCNT 1D/2D stride、标准 OBJ palette、翻转、affine/double-size，最大 128×128 RGBA |

`gpu_tiles` 的零号颜色按调色板显示；`gpu_tilemap` / `gpu_sprite` 的索引 0 为透明。
后两者返回 `composed=false`，不应用滚动、mosaic、窗口、优先级或背景混合。
地图和图像起点由调用者显式指定物理 bank/offset；`gpu_sprite.offset` 已是精灵源数据首字节，
不会再加 OAM tile index。1D tile 行紧密排列，DS 2D 模式每 8 像素行固定跨 1024 字节，
包括 8bpp；DISPCNT 的 base/boundary 映射仍由调用者解析。

disabled、保留 shape、bitmap OBJ 及需要扩展 palette 的图像明确拒绝，不生成伪图。
半透明 OBJ 显示混合前颜色，OBJ-window 输出白色覆盖 mask。尚未提供完整图层/精灵合成、
扩展调色板或 3D 纹理查看器。资源 RGB555 用完整 0..255 范围展开，可能与最终屏幕颜色不同。

## 五、安全内存与真正反编译

| 工具 | 参数 | 视图与作用 |
| --- | --- | --- |
| `memory_peek` | `address, length=256, cpu=0` | 安全 CPU 数据视图，含 ARM9 ITCM/DTCM overlay；`access=debug_peek_data_view` |
| `code_peek` | `address, length=256, cpu=0` | 指令 backing，含 ITCM、排除 DTCM；`access=debug_peek_instruction_backing` |
| `memory_poke` | `address, hex_data, cpu=0, expected_hex=null` | 对应数据视图写入；保持指令预取不变；`access=debug_poke_data_view` |
| `code_patch` | `address, hex_data, cpu=0, expected_hex=null` | 指令 backing 修补，刷新两核中物理匹配的预取；`access=debug_patch_instruction_backing` |

读取返回完整 hex、SHA256 和帧号，单次 1..4096 字节；支持 DS RAM 镜像、当前 WRAM 分配、
对应 TCM 视图和 BIOS backing。BIOS 是调试映像，不模拟 ARM7 PC 读保护。两种 peek 都不是
I-cache 或取指流水线快照；同一 ARM9 地址的数据/代码视图可能因 DTCM overlay 而不同。

写入同样最多 4096 字节，拒绝 BIOS、MMIO、VRAM/OAM、卡带、未知区和 DSi；无总线 fallback。
推荐先用匹配视图 peek，再把返回 hex 作为同长度 `expected_hex` 传入写工具。前置值不符时
报 `EXPECTED_BYTES_MISMATCH` 且不写入；成功结果附原值/新值及 hash、帧号和读回验证。
此保护比较当前字节，不是跨进程事务或完整历史版本校验。`memory_poke` 不刷新预取，
需要影响下一次代码执行时用 `code_patch`；代码补丁不靠执行 CPU 来刷新状态。
默认解释器已作为当前验证目标，JIT 行为未验证。原 `read_memory`/`write_memory` 仍保留
有副作用的总线语义，不覆盖 TCM overlay，不能当作以上工具的别名。

`analysis_status` 检查可选后端，`decompile_bytes` 反编译显式机器码，`decompile_memory`
默认通过安全指令 backing（`view="instruction"`）取快照，也可显式选择数据视图。
返回真实 Ghidra 伪 C、CFG、函数信息、版本和
输入哈希。参数、配置、例子与限制见 [ANALYSIS.md](ANALYSIS.md)。Capstone `disassemble`
仍单独提供反汇编，二者不混名。

## 六、C API（libmelonds_mcp）

Python 层之下是扁平 C API，可供其他语言直接绑定。完整签名见 [mcp/shim/mcp_shim.cpp](../shim/mcp_shim.cpp)。

主要函数族（前缀 `melonds_`）：

- 生命周期：`init / free / open / pause / resume / reset / running / cycle`
- 输入：`input_keypad_update / input_keypad_get / input_set_touch_pos / input_release_touch / set_lid_closed`
- 内存：`memory_read8/16/32 / memory_write8/16/32 / memory_read_block / memory_write_block`（均带 `cpu` 参数）
- 安全内存：`memory_peek_block / code_peek_block`（数据/指令 backing；长度 1..4096；失败返回 0）
- 安全写入：`memory_poke_block(cpu,address,length,source,view)`，`view=0` 数据、`view=1` 代码修补；失败返回 0
- 寄存器：`debug_get_registers / debug_write_register / get_pc`
- 断点：`debug_bp_add / debug_bp_remove / debug_bp_set_enabled / debug_bp_clear / debug_bp_list`
- 观察点：`debug_wp_add / debug_wp_remove / debug_wp_clear / debug_wp_list / debug_wp_events`
- 追踪：`debug_trace_start / debug_trace_stop / debug_trace_count / debug_trace_drain`
- 单步：`debug_step_request / debug_step_pending`
- 命中：`debug_break_info / debug_break_ack`
- 状态：`get_status / get_cycles / get_rom_info / screenshot`
- GPU：`gpu_read / gpu_state`（复制原始存储，调用方负责串行化）
- 其他：`savestate_* / backup_* / audio_* / set_skip_render / set_jit`

## 七、已知限制

所有地址限制为 uint32 且禁止范围溢出；单次内存读取/字节写入最多 4096 字节；帧推进上限 3600；反汇编上限 1024 条；单步上限 100000 条。整数/布尔/列表不进行隐式字符串转换，未知参数被拒绝。以下是原 fork 的覆盖边界，核心整合改变覆盖时应配合真实 native 测试更新，不能由 Python mock 测试推断已修复。

1. **JIT 与调试互斥**：断点/单步/追踪/观察点激活期间自动切换到解释器模式（速度下降），停用后自动恢复 JIT；本轮真实验证使用默认 JIT OFF，不代表 JIT 已验收。
2. **DMA 不触发观察点**：观察点仅覆盖 CPU 数据访问（LDR/STR 等），DMA 传输不经过钩子。
3. **观察点验证范围**：LDM/STM 顺序变体（32S 路径）已补钩子；当前真实回归覆盖双核 word store 与单字节观察区间重叠，完整指令/宽度矩阵仍待补测。
4. **断点暂停可能发生在帧中间**：恢复执行后继续当前帧；ARM7 断点处的帧内 savestate 保存/恢复已通过真实重放测试，截图仍表示最近完成帧。
5. **DSi 模式**：内存观察点挂钩于 DS 基类路径，DSi 覆写路径未完全覆盖。
6. **反编译范围**：已集成真实 Ghidra 后端，但每次只映射给定的最多 4096 字节；没有全 ROM 符号/类型、外部函数或运行时指令缓存的自动重建。

## 八、agent 集成

`.mcp.json` 示例（Trae / Claude Desktop / Cursor 通用格式）：

```json
{
  "mcpServers": {
    "melonds": {
      "command": "python3",
      "args": ["-m", "melonds_mcp"],
      "env": {
        "PYTHONPATH": "/path/to/melonDS/mcp/python",
        "MELONDS_MCP_LIB": "/path/to/melonDS/build/libmelonds_mcp.dylib",
        "MELONDS_MCP_ROM": "/path/to/game.nds"
      }
    }
  }
}
```

建议 agent 工作模式：

- **探索**：`load_rom` → `advance_frames` → `screenshot` 循环理解游戏；
- **逆向**：`watchpoint_add` 定位变量写入点 → `run_until_break` → `disassemble` + `read_registers` 分析逻辑；
- **自动化**：`advance_frames_until` + `press_buttons`/`tap_screen` 编写 Bot；
- **内存修改**：`memory_peek` → `memory_poke(expected_hex=原值)`；代码修补使用 `code_peek` → `code_patch`。
