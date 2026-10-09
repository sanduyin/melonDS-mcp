# melonDS-MCP 协议与工具契约

状态：历史 `mcp-server/` GDB 后端与 `src/mcp/` IPC 原型契约，**不是当前默认 MCP 工具契约**。

2026-09-04：默认无头服务在 `mcp/`，直接调用共享核心；其工具见
[当前 API 文档](../../mcp/docs/API.md)，启动和实测范围见 [使用说明](../../mcp/README.md)。
下文保留为旧方案参考，不用于描述新服务的能力或返回结构。

适用上游固定点：`906e9ebb27da8c6a715cd7abab4abfe8a8d29427`

本文定义两层协议：Agent 看见的 MCP 工具契约，以及未来 Python facade 与 melonDS native bridge 之间的本地 IPC。两层不能混为一谈；IPC 可以演进，而 MCP 工具应尽量保持兼容。

## 1. MCP transport

当前 server 使用标准 MCP stdio transport：

- stdin/stdout 仅承载 MCP 协议；
- 日志、诊断和 native child output 只能写 stderr；
- server 本身不监听 TCP；
- MCP host 负责进程生命周期；
- 同一 server 进程当前拥有一个 melonDS debugger session。

所有 melonDS 工具结果使用相同外壳：

```json
{
  "snapshot": {
    "attached": true,
    "active_core": "arm9",
    "state_version": "7",
    "stop_id": "3",
    "consistency": "active_core_gdb_stop",
    "backend": "transitional_gdb_rsp"
  },
  "data": {}
}
```

计数器使用十进制字符串。调用方必须检查 `backend` 和 `consistency`，不能把过渡后端的单核停止误当作全局快照。

## 2. 当前 MCP 工具

| 工具 | 状态要求 | 修改状态 | 并发前置条件 | 说明 |
| --- | --- | --- | --- | --- |
| `emulator_status` | 任意 | 否 | 无 | attachment、active core、每核状态、版本和限制 |
| `emulator_attach` | detached | 是 | 无 | 连接 ARM9/ARM7 RSP；默认 loopback |
| `emulator_detach` | 任意 | 是 | 无 | 关闭 socket，不终止 melonDS |
| `emulator_activate_core` | attached | 是 | 无 | 恢复另一 stopped core 后暂停目标 core |
| `emulator_resume` | 有 active stopped core | 是 | 无 | 返回后目标处于 running |
| `cpu_step` | 指定 core 为 active stopped | 是 | `expected_stop_id` | 1..1000 条 ARM/Thumb 指令 |
| `cpu_register_read` | 指定 core 为 active stopped | 否 | 无 | PC 为上游 pipeline-corrected 地址 |
| `cpu_register_write` | 指定 core 为 active stopped | 是 | `expected_stop_id` | 可选读回验证；完整 CPSR 写入在过渡后端硬拒绝 |
| `memory_read` | 指定 core 为 active stopped | 否 | 无 | hex/base64，有长度上限，MMIO 读取拒绝 |
| `memory_write` | 指定 core 为 active stopped | 是 | `expected_stop_id` | before-image、验证、失败回滚；MMIO 硬拒绝 |
| `breakpoint_set` | 指定 core 为 active stopped | 是 | `expected_stop_id` | ARM kind=4，Thumb kind=2，auto 读取 CPSR T bit |
| `disassemble` | 指定 core 为 active stopped | 否 | 无 | Capstone ARM/Thumb，最多 256 条；MMIO 取指硬拒绝 |

“没有 `expected_stop_id` 的 read”并不代表跨运行状态可用；当前 backend 会确认指定 core 仍是 active stopped，否则返回 session error。工具不会隐式暂停。

### 2.1 Mutation 模式

推荐 Agent 调用序列：

```text
emulator_status
  → emulator_activate_core(core)
  → emulator_status / read tools
  → mutation(expected_stop_id = snapshot.stop_id)
  → inspect returned snapshot
```

不要缓存旧 `stop_id` 跨越 step、resume、新断点命中或 active-core 切换。一次 mutation 若返回 `STALE_STOP`，Agent 必须重新读取状态和相关证据后重新决策，而不是自动替换成最新 id 重试。

### 2.2 稳定错误前缀

当前 facade 将预期错误映射为 model-readable 前缀：

| 前缀 | 含义 | 调用方动作 |
| --- | --- | --- |
| `INVALID_ARGUMENT` | schema 之外的值、越界地址/长度、不安全区域 | 修正请求；不要原样重试 |
| `STALE_STOP` | `expected_stop_id` 已过期 | 重新观察状态和证据，再决定是否重试 |
| `SESSION_STATE` | 未 attach、core 未激活或运行状态不合法 | 显式执行 attach/activate/pause 流程 |
| `DEBUGGER_CONNECTION` | RSP 端口不可达、断开或超时 | 检查 melonDS、端口、JIT/GDB 设置 |
| `DEBUGGER_PROTOCOL` | checksum、packet 或 target response 异常 | 保留诊断并重建 session |
| `MELONDS_ERROR` | 其他受控 domain error | 根据消息处理，不作无界重试 |

未知 server exception 应作为内部错误处理，不能把 traceback、路径或 secret 放入 MCP payload。

## 3. 目标 MCP 快照

native backend 上线后，公共结果外壳扩展而不删除现有字段：

```json
{
  "snapshot": {
    "attached": true,
    "backend": "native_bridge_v1",
    "consistency": "global_stop_the_world",
    "session_id": "d1b8d757...",
    "run_state": "stopped",
    "state_version": "184",
    "stop_id": "12",
    "frame_number": "9831",
    "stop_reason": {
      "kind": "breakpoint",
      "core": "arm9",
      "pc": "0x02001a34"
    }
  },
  "data": {}
}
```

兼容规则：

- 调用方忽略未知字段；
- server 不改变现有字段类型；
- 新语义使用新 `backend`/`consistency` 值；
- 只有 `global_stop_the_world` 才允许把双 CPU、内存和 GPU device summary 视为同一时刻；
- native mutation 同时校验 `session_id` 以及适用的 `expected_stop_id`/`expected_state_version`。

## 4. Native IPC v1 framing

### 4.1 端点

- Windows：用户 ACL 保护的 named pipe；名称包含随机实例标识。
- Linux/macOS：权限 `0600` 的 Unix-domain socket，位于用户运行时目录。
- 禁止默认 TCP fallback。
- facade 通过一次性 endpoint descriptor 获取 pipe/socket 名称和 secret；descriptor 不写入 MCP 输出。

### 4.2 固定 32-byte header

每个 frame 以完全固定的 32-byte little-endian header 开始：

| Offset | Size | Field | 固定值/语义 |
| ---: | ---: | --- | --- |
| 0 | 4 | `magic` | ASCII `MDSB`（字节 `4d 44 53 42`） |
| 4 | 2 | `version` | `u16 LE`，v1 固定为 `1` |
| 6 | 2 | `type` | `u16 LE`：`1=request`、`2=response`、`3=event`、`4=cancel` |
| 8 | 4 | `flags` | `u32 LE`，v1 必须为 `0` |
| 12 | 8 | `request_id` | `u64 LE`，关联 request/response；event/cancel 语义见下文 |
| 20 | 4 | `json_length` | `u32 LE`，紧随 header 的 UTF-8 JSON object 字节数 |
| 24 | 4 | `binary_length` | `u32 LE`，JSON 后二进制 payload 的字节数 |
| 28 | 4 | `reserved` | `u32 LE`，v1 必须为 `0` |

完整 wire layout 为：

```text
+--------------------------+-------------------------+---------------------------+
| fixed MDSB header        | UTF-8 JSON object       | binary payload (optional) |
| exactly 32 bytes         | header.json_length      | header.binary_length      |
+--------------------------+-------------------------+---------------------------+
```

规则：

- magic、version、type、flags 或 reserved 非法时，peer 必须 fail closed；不得猜测或自动降级到旧 framing。
- JSON 默认上限 1 MiB，binary 默认上限 64 MiB；具体 operation 可以声明更小上限。
- JSON 必须是 UTF-8 object；空字节串、array、scalar、重复 key、NaN 和 Infinity 非法。无参数 frame 使用 `{}`。
- 收到完整 32-byte header、`json_length` 字节 JSON 和 `binary_length` 字节 payload 前不得执行请求。
- JSON 地址使用 `0x` 十六进制字符串，跨进程计数器使用十进制字符串。
- 一个连接可以有多个 outstanding request；actor 对 mutation 保持提交顺序。
- `type=1` 时 `request_id` 必须在连接内非零且唯一；对应 `type=2` response 原样回传该值。
- `type=3` event 没有 request，header 的 `request_id` 承载连接内单调递增的 event sequence。
- `type=4` cancel 的 header `request_id` 指向要取消的 outstanding request，JSON 通常为 `{}`，且不得带 binary payload。

### 4.3 Request JSON

```json
{
  "operation": "memory.peek",
  "session_id": "d1b8d757...",
  "deadline_ms": "5000",
  "expected": {
    "run_state": "stopped",
    "stop_id": "12",
    "state_version": "184"
  },
  "params": {
    "core": "arm9",
    "address": "0x02000000",
    "length": 4096
  }
}
```

- 此 JSON 前的固定 header 使用 `type=1`、非零唯一 `request_id`、对应的 `json_length` 和 `binary_length=0`。
- `deadline_ms` 是相对接收时间的预算，不是 wall-clock timestamp。
- `expected` 只放 operation 要求的前置条件；mutation 不得缺少其必需字段。
- `params` 只含控制 metadata；批量 write bytes 放 binary payload。
- 未知必需 operation 返回 `UNSUPPORTED_OPERATION`，不会关闭连接。

### 4.4 Success response

```json
{
  "ok": true,
  "snapshot": {
    "session_id": "d1b8d757...",
    "run_state": "stopped",
    "state_version": "184",
    "stop_id": "12",
    "frame_number": "9831",
    "consistency": "global_stop_the_world"
  },
  "result": {
    "address": "0x02000000",
    "length": 4096,
    "sha256": "..."
  }
}
```

此 JSON 前的固定 header 使用 `type=2`，原样回传 request header 的 `request_id`，并设置 `binary_length=4096`。所有 success response 都带 actor 执行完成时的 snapshot。内存、PNG/raw RGBA、trace chunk 和 savestate 等大数据优先放 binary payload；JSON 只放 metadata、长度和 hash。

### 4.5 Error response

```json
{
  "ok": false,
  "error": {
    "code": "STALE_STOP",
    "message": "expected stop 11, current stop is 12",
    "retryable": false,
    "details": {
      "expected_stop_id": "11",
      "current_stop_id": "12"
    }
  },
  "snapshot": {
    "session_id": "d1b8d757...",
    "run_state": "stopped",
    "state_version": "184",
    "stop_id": "12",
    "frame_number": "9831",
    "consistency": "global_stop_the_world"
  }
}
```

错误 response 的固定 header 使用 `type=2`、原 request 的 `request_id` 和 `binary_length=0`。

错误码集合至少包括：

- `INVALID_ARGUMENT`
- `AUTHENTICATION_FAILED`
- `UNSUPPORTED_VERSION`
- `UNSUPPORTED_OPERATION`
- `SESSION_MISMATCH`
- `INVALID_RUN_STATE`
- `STALE_STOP`
- `STALE_STATE`
- `UNSAFE_ADDRESS_SPACE`
- `LIMIT_EXCEEDED`
- `DEADLINE_EXCEEDED`
- `CANCELLED`
- `NOT_AVAILABLE`
- `INTERNAL_ERROR`

`INTERNAL_ERROR` 不携带 C++ stack、绝对路径或 secret。完整诊断只进入本地受控日志，并用 opaque diagnostic id 关联。

### 4.6 Events

native bridge 可以发送：

```json
{
  "event": "emulator.stopped",
  "snapshot": {},
  "data": {}
}
```

event 固定 header 使用 `type=3`，其 `request_id` 字段作为连接内单调递增的 event sequence。v1 事件候选：`emulator.started`、`emulator.stopped`、`emulator.resumed`、`frame.completed`、`session.replaced`、`bridge.shutting_down`。事件用于通知，不能替代 mutation 的 response。facade 断线重连后必须先 `session.describe`，不能仅从事件恢复状态。

## 5. Native operation families（目标）

| Family | 示例 operation | 一致性/权限 |
| --- | --- | --- |
| Session | `session.describe`, `session.capabilities` | read-only，任意状态 |
| Execution | `emulator.pause`, `emulator.resume`, `emulator.step_frame`, `cpu.step` | actor mutation；step 要求 stopped generation |
| Lifecycle | `emulator.reset`, `rom.load`, `state.save`, `state.load` | 高权限、路径 allowlist、state precondition |
| CPU | `cpu.registers.read`, `cpu.register.write` | stopped；安全处理 PC/CPSR/banked regs |
| Memory | `memory.peek`, `memory.poke`, `memory.bus_access` | peek 无副作用；bus_access 单独 dangerous 权限 |
| Breakpoints | `breakpoint.upsert`, `breakpoint.remove`, `watchpoint.upsert` | stopped mutation；返回稳定 id |
| Input | `input.set`, `input.touch`, `input.release_all` | frame-aligned mutation，可预约 frame |
| Graphics | `screen.capture`, `gpu.snapshot`, `vram.read`, `texture.capture` | capture barrier，返回 frame/version metadata |
| Trace | `trace.start`, `trace.stop`, `trace.chunk.read` | 有界 ring buffer，明确 dropped count |

operation capability 通过 `session.capabilities` 协商。facade 不得因为 operation 不可用而模拟出更弱但看似相同的保证，例如不能用 RSP 单核 stop 冒充 `global_stop_the_world`。

## 6. 内存协议

### 6.1 `memory.peek`

- 默认且推荐的观察操作；
- 只允许 native bridge 明确标记为 side-effect-free 的区域；
- 必须返回实际映射区域、core/view、capture version 和 hash；
- 跨 region 请求默认拒绝，除非所有分段都可安全 peek 并在结果中列出；
- 长读取使用 snapshot token + offset 分页或 binary payload。

### 6.2 `memory.poke`

- 要求 global stopped、`session_id`、`expected_stop_id` 和 `expected_state_version`；
- 请求 bytes 放 binary payload；
- 写入前验证整个范围，再执行，不能先写一半再发现越界；
- 成功后执行必要的 code/JIT/cache invalidation；
- 响应返回 before/after hash、modified ranges 和新 `state_version`。

### 6.3 `memory.bus_access`

- 与 debug peek/poke 分开的 dangerous operation；
- request 必须显式声明 `acknowledge_side_effects=true`；
- 逐项报告实际 bus transaction；
- 不使用 read-only MCP annotation；
- 不承诺 rollback、idempotence 或原子性。

当前 RSP `memory_read`/`memory_write` 不具备这套完整区分，因此其结果仅在 `backend=transitional_gdb_rsp` 范围内解释。

## 7. 图像和分析结果

`screen.capture`/GPU 图像 binary payload 的 metadata 至少包括：

```json
{
  "screen": "top",
  "width": 256,
  "height": 192,
  "stride": 1024,
  "pixel_format": "rgba8",
  "encoding": "raw",
  "frame_number": "9831",
  "state_version": "184",
  "sha256": "..."
}
```

PNG 使用 `encoding=png` 且 `pixel_format` 描述解码后的格式。上下屏批量 capture 必须来自同一个 capture barrier。

反汇编结果为结构化 instruction list，包含 address、bytes、mnemonic、operands、mode 和可选 symbol/branch target。真正反编译结果使用不同 operation/resource，至少携带 analyzer、analyzer_version、input_sha256、load_map 和 confidence/provenance；不得把 Capstone 文本包装成 decompilation。

## 8. 兼容性与版本协商

- IPC 固定 header 的 `version` 不为 `1` 时拒绝 frame/连接；v1 内的可选 feature 通过 capabilities 协商。
- MCP 工具新增 optional 字段是兼容变更；删除字段、改变字段类型或收紧已有成功输入需要 major 迁移计划。
- native bridge 和 facade 各自报告 build id、git commit 和 capability set。
- 固定上游提交只是当前设计/测试基线；升级上游时必须重跑 ABI/API audit、GDB/native integration 和图形一致性测试。
- RSP backend 在 native backend 达到功能和回归门槛前保留为诊断 fallback；不会悄悄自动降级。fallback 必须在 snapshot 中可见。
