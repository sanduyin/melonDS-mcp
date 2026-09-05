/*
    MCPDebug.h — MCP 调试钩子核心模块（melonDS-mcp fork 新增）

    提供指令级断点、单步执行、指令追踪与数据观察点功能。
    所有钩子经由解释器主循环与 CPU 数据访问汇聚点调用，
    在全部功能禁用时仅产生一次布尔分支开销（≈零成本）。

    线程模型：模拟器为单线程执行，管理接口仅在模拟暂停时由
    shim 层调用，因此内部无需加锁。

    Copyright (C) 2026 melonDS-mcp contributors
    Adapted from https://github.com/sanduyin/melonDS-mcp
    (commit 3b39290543904bbafe62ec5c4f208b69172212ec).
    Licensed under GPLv3 (same as melonDS)
*/

#ifndef MCPDEBUG_H
#define MCPDEBUG_H

#include "types.h"
#include <vector>

namespace melonDS
{
class ARM;
}

namespace melonDS::MCPDebug
{

// ── 常量 ──

constexpr int MaxBreakpoints  = 64;          // 断点数量上限
constexpr int MaxWatchpoints  = 64;          // 观察点数量上限
constexpr u32 TraceBufferSize = 65536;       // 指令追踪环形缓冲容量
constexpr u32 WatchEventBufferSize = 4096;   // 观察点事件环形缓冲容量

// 观察点触发类型（可按位组合）
enum WatchKind : u8
{
    WatchRead  = 1,
    WatchWrite = 2,
    WatchRW    = 3,
};

// 断点/观察点命中原因
enum BreakReason : u8
{
    BreakNone       = 0,
    BreakBreakpoint = 1,  // PC 断点
    BreakWatchpoint = 2,  // 数据观察点
    BreakStep       = 3,  // 单步完成
};

// ── 数据结构 ──

struct Breakpoint
{
    int ID;
    u32 CPU;      // 0=ARM9, 1=ARM7
    u32 Addr;     // 指令地址（真实 PC）
    bool Enabled;
};

struct Watchpoint
{
    int ID;
    u32 CPU;      // 0=ARM9, 1=ARM7
    u32 AddrStart;
    u32 AddrEnd;  // 含端点
    u8 Kind;      // WatchKind
    bool Enabled;
};

// 追踪条目：一条已到达但尚未执行的指令
struct TraceEntry
{
    u32 CPU;
    u32 PC;
    u32 Instr;    // 指令机器码（NextInstr[0]）
    u32 CPSR;
};

// 观察点事件
struct WatchEvent
{
    u32 CPU;
    u32 Addr;
    u32 PC;       // 触发访问时的指令地址
    u8 Kind;      // WatchKind 实际触发方向
    u8 Size;      // 访问宽度（字节）
    u32 Value;    // 写入值；读访问时为 0
};

// 当前命中状态（供 shim 查询）
struct BreakInfo
{
    bool Hit;
    u32 CPU;
    u32 PC;
    u8 Reason;    // BreakReason
    u32 Addr;     // 观察点命中地址（BreakWatchpoint 时有效）
    int ID;       // 命中的断点/观察点 ID
};

// ── 热路径接口（由 ARM.cpp / CP15.cpp 调用）──

// 指令钩子是否激活（存在断点/单步请求/追踪开启）
bool AnyHooksActive();
// 数据访问钩子是否激活（存在观察点）
bool DataHooksActive();

// 指令边界钩子。返回 true 时解释器应立即跳出执行循环（断点/单步到达）。
// 在每条指令执行前调用（与 GdbCheckC 相同的插入点）。
bool InstructionHook(ARM* cpu);

// 数据访问钩子。观察点命中时记录事件并置挂起断点标志，
// 由下一条指令边界的 InstructionHook 统一触发暂停。
void DataReadHook(ARM* cpu, u32 addr, int size);
void DataWriteHook(ARM* cpu, u32 addr, u32 value, int size);

// ── 管理接口（仅模拟暂停时由 shim 调用）──

// 清除运行时状态（单步请求、命中标志、缓冲区）。
// 断点/观察点表保留。ROM 重载/重置时由 shim 调用。
void ResetRuntimeState();

// 断点管理。AddBreakpoint 返回新 ID（重复添加同一 (cpu,addr) 返回已有 ID），失败返回 -1。
int  AddBreakpoint(u32 cpu, u32 addr);
bool RemoveBreakpoint(int id);
bool SetBreakpointEnabled(int id, bool enabled);
void ClearBreakpoints(u32 cpu);             // cpu=0xFFFFFFFF 表示全部
std::vector<Breakpoint> GetBreakpoints();

// 观察点管理
int  AddWatchpoint(u32 cpu, u32 addr, u32 size, u8 kind); // kind 为 WatchKind 组合
bool RemoveWatchpoint(int id);
bool SetWatchpointEnabled(int id, bool enabled);
void ClearWatchpoints(u32 cpu);             // cpu=0xFFFFFFFF 表示全部
std::vector<Watchpoint> GetWatchpoints();

// 单步：请求指定 CPU 执行 count 条指令后在下一条指令边界暂停。
// 实际推进由外部调用 RunFrame 完成（shim 的 cycle/step 函数）。
void RequestStep(u32 cpu, u32 count);
bool StepPending();

// 指令追踪
// cpuMask: bit0=ARM9 bit1=ARM7；addrStart/addrEnd 为 0~0xFFFFFFFF 表示不过滤
void TraceStart(u32 cpuMask, u32 addrStart, u32 addrEnd);
void TraceStop();
bool TraceActive();
u32  TraceCount();                          // 当前缓冲内条目数
u32  DrainTrace(TraceEntry* out, u32 max);  // 取出并清空，返回实际条数

// 观察点事件
u32  DrainWatchEvents(WatchEvent* out, u32 max);

// 命中状态
BreakInfo GetBreakInfo();
void AckBreak(); // Clear the hit; let the stopped breakpoint instruction run once.

}

#endif // MCPDEBUG_H
