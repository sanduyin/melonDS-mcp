# melonDS-mcp API 文档

melonDS-mcp 是 melonDS 的本地分支版本，集成了完整的 MCP（Model Context Protocol）服务器，允许 AI agent 通过标准化接口控制模拟器、调试游戏程序并查看运行状态。

## 架构总览

```
AI agent (Claude / 其他 MCP 客户端)
        │  MCP (stdio, JSON-RPC)
        ▼
melonds_mcp Python 服务器 (FastMCP)
   ├── tools_control  操控系统
   ├── tools_debug    调试工具集
   └── tools_status   状态查看组件
        │  ctypes
        ▼
libmelonds_mcp.dylib / .so  (C shim 层)
        │  C++ 直接调用
        ▼
melonDS 核心 (含 MCPDebug 调试钩子)
```

- **通信方式**：MCP over stdio（标准输入输出），与 Claude Desktop / Trae / Cursor 等 MCP 客户端兼容。
- **执行模型**：模拟器与 MCP 服务器同进程同步执行。每次工具调用直接推进模拟，无竞态。
- **调试实现**：指令级断点/单步/追踪通过核心新增的 `src/MCPDebug` 钩子实现（仅在解释器模式下生效，激活调试功能时自动关闭 JIT，全部停用后自动恢复）。

## 环境变量

| 变量 | 说明 | 默认 |
|---|---|---|
| `MELONDS_MCP_LIB` | libmelonds_mcp 库路径 | 仓库 `build/` 下自动查找 |
| `MELONDS_MCP_ROM` | 服务器启动时自动加载的 ROM 路径 | 无 |

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
| `advance_frames` | `frames=1` | 推进 N 帧（默认跳过渲染提速） |
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
| `write_register` | `cpu, name, value` | 写入寄存器（r0-r12/sp/lr/pc/cpsr） |
| `get_pc` | `cpu=0` | 读取当前 PC |
| `disassemble` | `address, count=10, cpu=0, thumb=None` | 反汇编（需 capstone；thumb 缺省按 CPSR T 位判断） |

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
breakpoint_add(cpu=0, address=0x02000123)   # ARM9 断点
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
| `screenshot` | `screen="both", format="png"` | 截图返回 base64 PNG（top/bottom/both）；format=rgb_hex 返回原始像素十六进制 |
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

## 四、C API（libmelonds_mcp）

Python 层之下是扁平 C API，可供其他语言直接绑定。完整签名见 [mcp/shim/mcp_shim.cpp](../shim/mcp_shim.cpp)。

主要函数族（前缀 `melonds_`）：

- 生命周期：`init / free / open / pause / resume / reset / running / cycle`
- 输入：`input_keypad_update / input_keypad_get / input_set_touch_pos / input_release_touch / set_lid_closed`
- 内存：`memory_read8/16/32 / memory_write8/16/32 / memory_read_block / memory_write_block`（均带 `cpu` 参数）
- 寄存器：`debug_get_registers / debug_write_register / get_pc`
- 断点：`debug_bp_add / debug_bp_remove / debug_bp_set_enabled / debug_bp_clear / debug_bp_list`
- 观察点：`debug_wp_add / debug_wp_remove / debug_wp_clear / debug_wp_list / debug_wp_events`
- 追踪：`debug_trace_start / debug_trace_stop / debug_trace_count / debug_trace_drain`
- 单步：`debug_step_request / debug_step_pending`
- 命中：`debug_break_info / debug_break_ack`
- 状态：`get_status / get_cycles / get_rom_info / screenshot`
- 其他：`savestate_* / backup_* / audio_* / set_skip_render / set_jit`

## 五、已知限制

1. **JIT 与调试互斥**：断点/单步/追踪/观察点激活期间自动切换到解释器模式（速度下降），停用后自动恢复 JIT。
2. **DMA 不触发观察点**：观察点仅覆盖 CPU 数据访问（LDR/STR 等），DMA 传输不经过钩子。
3. **LDM/STM 尾部访问不触发观察点**：顺序变体访问（32S 路径）未挂钩。
4. **断点暂停可能发生在帧中间**：恢复执行后当前帧继续完成，savestate 建议在帧边界（非暂停态）保存。
5. **DSi 模式**：内存观察点挂钩于 DS 基类路径，DSi 覆写路径未完全覆盖。
6. **反编译扩展**：当前提供反汇编（capstone）；静态反编译建议在 agent 侧集成 Ghidra headless / angr，通过 `read_memory`/`read_block` 获取 ROM 与 RAM 数据。

## 六、agent 集成

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
- **金手指**：`watch` 定位地址后用 `write_memory` 直接改值。
