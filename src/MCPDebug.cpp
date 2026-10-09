/*
    MCPDebug.cpp — MCP 调试钩子核心实现（melonDS-mcp fork 新增）

    断点/单步/追踪在解释器指令边界检查；观察点在 CPU 数据访问
    汇聚点检查，命中后记录事件并在下一条指令边界安全暂停。

    Copyright (C) 2026 melonDS-mcp contributors
    Adapted from https://github.com/sanduyin/melonDS-mcp
    (commit 3b39290543904bbafe62ec5c4f208b69172212ec).
    Licensed under GPLv3 (same as melonDS)
*/

#include "MCPDebug.h"
#include "ARM.h"
#include <cstring>
#include <algorithm>

namespace melonDS::MCPDebug
{

// ── 内部状态（模拟器单线程，无需加锁）──

static std::vector<Breakpoint> Breakpoints;
static std::vector<Watchpoint> Watchpoints;
static int NextBreakpointID = 1;
static int NextWatchpointID = 1;

// 指令级功能开关缓存（由管理接口维护，热路径只读）
static bool AnyBreakpoints = false;   // 存在启用的断点
static bool AnyWatchpoints = false;   // 存在启用的观察点

// 单步请求
static bool StepArmed = false;
static u32 StepCPU = 0;
static u32 StepRemaining = 0;

// 追踪状态
static bool TraceOn = false;
static u32 TraceCPUMask = 0;
static u32 TraceAddrStart = 0, TraceAddrEnd = 0xFFFFFFFF;
static TraceEntry TraceBuf[TraceBufferSize];
static u32 TraceHead = 0;   // 写入位置
static u32 TraceCount_ = 0; // 有效条数（封顶为 TraceBufferSize）

// 观察点事件环形缓冲
static WatchEvent WatchEventBuf[WatchEventBufferSize];
static u32 WatchEventHead = 0;
static u32 WatchEventCount_ = 0;

// 挂起断点（数据观察点命中，等待指令边界）
static bool PendingBreak = false;
static BreakInfo CurrentBreak = {};
static bool ResumeBreakpoint = false;
static u32 ResumeCPU = 0;
static u32 ResumePC = 0;
static u32 InstructionPC[2] = {};

// ── 热路径 ──

bool AnyHooksActive()
{
    // 注意：观察点命中产生的挂起断点必须经由指令边界钩子触发暂停，
    // 因此存在观察点时指令钩子也需保持激活。
    return AnyBreakpoints || StepArmed || TraceOn || AnyWatchpoints
        || PendingBreak || CurrentBreak.Hit || ResumeBreakpoint;
}

bool DataHooksActive()
{
    return AnyWatchpoints;
}

static inline u32 RealPC(ARM* cpu)
{
    // 调用发生在 prefetch 之前：R[15] 指向下一条指令地址 + 指令长度
    return cpu->R[15] - ((cpu->CPSR & 0x20) ? 2 : 4);
}

bool InstructionHook(ARM* cpu)
{
    // A hit is sticky for both CPUs until the control layer acknowledges it.
    // In particular an ARM7 return must never resume it in the catch-up loop.
    if (CurrentBreak.Hit)
        return true;

    u32 cpunum = cpu->Num; // 0=ARM9 1=ARM7
    u32 pc = RealPC(cpu);
    InstructionPC[cpunum] = pc;
    bool skipBreakpoint = false;
    if (ResumeBreakpoint && ResumeCPU == cpunum)
    {
        skipBreakpoint = ResumePC == pc;
        ResumeBreakpoint = false;
    }

    // 1) 追踪（先记录，再判断断点，保证断点指令本身也被追踪到）
    if (TraceOn && (TraceCPUMask & (1 << cpunum))
        && pc >= TraceAddrStart && pc <= TraceAddrEnd)
    {
        TraceEntry& e = TraceBuf[TraceHead];
        e.CPU = cpunum;
        e.PC = pc;
        e.Instr = cpu->NextInstr[0];
        e.CPSR = cpu->CPSR;
        TraceHead = (TraceHead + 1) % TraceBufferSize;
        if (TraceCount_ < TraceBufferSize) TraceCount_++;
    }

    // 2) 挂起的观察点命中 => 立即暂停
    if (PendingBreak)
    {
        PendingBreak = false;
        CurrentBreak.Hit = true;
        CurrentBreak.CPU = cpunum;
        CurrentBreak.PC = pc;
        // Reason/Addr/ID 已在 DataHook 中填写
        StepArmed = false;
        return true;
    }

    // 3) 断点匹配
    if (AnyBreakpoints && !skipBreakpoint)
    {
        for (const Breakpoint& bp : Breakpoints)
        {
            if (bp.Enabled && bp.Addr == pc && bp.CPU == cpunum)
            {
                CurrentBreak.Hit = true;
                CurrentBreak.CPU = cpunum;
                CurrentBreak.PC = pc;
                CurrentBreak.Reason = BreakBreakpoint;
                CurrentBreak.Addr = pc;
                CurrentBreak.ID = bp.ID;
                StepArmed = false;
                return true;
            }
        }
    }

    // 4) 单步计数：StepRemaining > 0 时放行并递减（已执行计数），
    //    归零后在下一条指令边界暂停。语义：step(n) 执行 n 条指令，
    //    在第 n+1 条指令之前暂停。
    if (StepArmed)
    {
        if (StepCPU == cpunum)
        {
            if (StepRemaining == 0)
            {
                CurrentBreak.Hit = true;
                CurrentBreak.CPU = cpunum;
                CurrentBreak.PC = pc;
                CurrentBreak.Reason = BreakStep;
                CurrentBreak.Addr = pc;
                CurrentBreak.ID = 0;
                StepArmed = false;
                return true;
            }
            StepRemaining--;
        }
    }

    return false;
}

static void RecordWatchEvent(u32 cpu, u32 addr, u32 pc, u8 kind, u8 size, u32 value)
{
    WatchEvent& e = WatchEventBuf[WatchEventHead];
    e.CPU = cpu;
    e.Addr = addr;
    e.PC = pc;
    e.Kind = kind;
    e.Size = size;
    e.Value = value;
    WatchEventHead = (WatchEventHead + 1) % WatchEventBufferSize;
    if (WatchEventCount_ < WatchEventBufferSize) WatchEventCount_++;
}

static void DataHook(ARM* cpu, u32 addr, u32 value, int size, bool write)
{
    if (size <= 0) return;
    u32 cpunum = cpu->Num;
    const u64 accessEnd = (u64)addr + (u32)size - 1;

    for (const Watchpoint& wp : Watchpoints)
    {
        if (!wp.Enabled || wp.CPU != cpunum) continue;
        if (accessEnd < wp.AddrStart || addr > wp.AddrEnd) continue;
        if (!(wp.Kind & (write ? WatchWrite : WatchRead))) continue;

        RecordWatchEvent(cpunum, addr, InstructionPC[cpunum],
                         write ? WatchWrite : WatchRead, (u8)size, value);

        // 置挂起标志，在下一条指令边界安全暂停
        if (!CurrentBreak.Hit)
        {
            PendingBreak = true;
            CurrentBreak.Hit = true;
            CurrentBreak.CPU = cpunum;
            CurrentBreak.PC = InstructionPC[cpunum];
            CurrentBreak.Reason = BreakWatchpoint;
            CurrentBreak.Addr = addr;
            CurrentBreak.ID = wp.ID;
            StepArmed = false;
        }
        return; // 一个访问只报告一次
    }
}

void DataReadHook(ARM* cpu, u32 addr, int size)
{
    DataHook(cpu, addr, 0, size, false);
}

void DataWriteHook(ARM* cpu, u32 addr, u32 value, int size)
{
    DataHook(cpu, addr, value, size, true);
}

// ── 管理接口 ──

void ResetRuntimeState()
{
    StepArmed = false;
    StepRemaining = 0;
    PendingBreak = false;
    CurrentBreak = {};
    ResumeBreakpoint = false;
    InstructionPC[0] = InstructionPC[1] = 0;
    TraceOn = false;
    TraceHead = 0;
    TraceCount_ = 0;
    WatchEventHead = 0;
    WatchEventCount_ = 0;
}

int AddBreakpoint(u32 cpu, u32 addr)
{
    if (cpu > 1) return -1;
    for (const Breakpoint& bp : Breakpoints)
        if (bp.CPU == cpu && bp.Addr == addr) return bp.ID;

    if ((int)Breakpoints.size() >= MaxBreakpoints) return -1;

    Breakpoint bp;
    bp.ID = NextBreakpointID++;
    bp.CPU = cpu;
    bp.Addr = addr;
    bp.Enabled = true;
    Breakpoints.push_back(bp);
    AnyBreakpoints = true;
    return bp.ID;
}

bool RemoveBreakpoint(int id)
{
    for (auto it = Breakpoints.begin(); it != Breakpoints.end(); ++it)
    {
        if (it->ID == id)
        {
            Breakpoints.erase(it);
            AnyBreakpoints = std::any_of(Breakpoints.begin(), Breakpoints.end(),
                                         [](const Breakpoint& b) { return b.Enabled; });
            return true;
        }
    }
    return false;
}

bool SetBreakpointEnabled(int id, bool enabled)
{
    for (Breakpoint& bp : Breakpoints)
    {
        if (bp.ID == id)
        {
            bp.Enabled = enabled;
            AnyBreakpoints = std::any_of(Breakpoints.begin(), Breakpoints.end(),
                                         [](const Breakpoint& b) { return b.Enabled; });
            return true;
        }
    }
    return false;
}

void ClearBreakpoints(u32 cpu)
{
    if (cpu == 0xFFFFFFFF)
        Breakpoints.clear();
    else
        Breakpoints.erase(std::remove_if(Breakpoints.begin(), Breakpoints.end(),
                          [cpu](const Breakpoint& b) { return b.CPU == cpu; }),
                          Breakpoints.end());
    AnyBreakpoints = std::any_of(Breakpoints.begin(), Breakpoints.end(),
                                 [](const Breakpoint& b) { return b.Enabled; });
}

std::vector<Breakpoint> GetBreakpoints() { return Breakpoints; }

int AddWatchpoint(u32 cpu, u32 addr, u32 size, u8 kind)
{
    if (cpu > 1) return -1;
    if (size == 0 || (u64)addr + size > 0x100000000ULL) return -1;
    if ((kind & WatchRW) == 0 || (kind & ~WatchRW) != 0) return -1;

    // 与现有观察点重叠视为重复
    u32 end = addr + size - 1;
    for (const Watchpoint& wp : Watchpoints)
        if (wp.CPU == cpu && addr <= wp.AddrEnd && end >= wp.AddrStart) return -1;

    if ((int)Watchpoints.size() >= MaxWatchpoints) return -1;

    Watchpoint wp;
    wp.ID = NextWatchpointID++;
    wp.CPU = cpu;
    wp.AddrStart = addr;
    wp.AddrEnd = end;
    wp.Kind = kind;
    wp.Enabled = true;
    Watchpoints.push_back(wp);
    AnyWatchpoints = true;
    return wp.ID;
}

bool RemoveWatchpoint(int id)
{
    for (auto it = Watchpoints.begin(); it != Watchpoints.end(); ++it)
    {
        if (it->ID == id)
        {
            Watchpoints.erase(it);
            AnyWatchpoints = std::any_of(Watchpoints.begin(), Watchpoints.end(),
                                         [](const Watchpoint& w) { return w.Enabled; });
            return true;
        }
    }
    return false;
}

bool SetWatchpointEnabled(int id, bool enabled)
{
    for (Watchpoint& wp : Watchpoints)
    {
        if (wp.ID == id)
        {
            wp.Enabled = enabled;
            AnyWatchpoints = std::any_of(Watchpoints.begin(), Watchpoints.end(),
                                         [](const Watchpoint& w) { return w.Enabled; });
            return true;
        }
    }
    return false;
}

void ClearWatchpoints(u32 cpu)
{
    if (cpu == 0xFFFFFFFF)
        Watchpoints.clear();
    else
        Watchpoints.erase(std::remove_if(Watchpoints.begin(), Watchpoints.end(),
                          [cpu](const Watchpoint& w) { return w.CPU == cpu; }),
                          Watchpoints.end());
    AnyWatchpoints = std::any_of(Watchpoints.begin(), Watchpoints.end(),
                                 [](const Watchpoint& w) { return w.Enabled; });
}

std::vector<Watchpoint> GetWatchpoints() { return Watchpoints; }

void RequestStep(u32 cpu, u32 count)
{
    if (cpu > 1 || count == 0) return;
    AckBreak();
    StepArmed = true;
    StepCPU = cpu;
    StepRemaining = count; // 放行 count 条指令，随后在指令边界暂停
}

bool StepPending() { return StepArmed; }

void TraceStart(u32 cpuMask, u32 addrStart, u32 addrEnd)
{
    TraceCPUMask = cpuMask & 3;
    TraceAddrStart = addrStart;
    TraceAddrEnd = addrEnd;
    TraceHead = 0;
    TraceCount_ = 0;
    TraceOn = TraceCPUMask != 0;
}

void TraceStop() { TraceOn = false; }

bool TraceActive() { return TraceOn; }

u32 TraceCount() { return TraceCount_; }

u32 DrainTrace(TraceEntry* out, u32 max)
{
    u32 n = std::min(max, TraceCount_);
    // 环形缓冲：从最旧条目开始拷贝
    u32 oldest = (TraceHead + TraceBufferSize - TraceCount_) % TraceBufferSize;
    for (u32 i = 0; i < n; i++)
        out[i] = TraceBuf[(oldest + i) % TraceBufferSize];
    TraceCount_ -= n;
    return n;
}

u32 DrainWatchEvents(WatchEvent* out, u32 max)
{
    u32 n = std::min(max, WatchEventCount_);
    u32 oldest = (WatchEventHead + WatchEventBufferSize - WatchEventCount_) % WatchEventBufferSize;
    for (u32 i = 0; i < n; i++)
        out[i] = WatchEventBuf[(oldest + i) % WatchEventBufferSize];
    WatchEventCount_ -= n;
    return n;
}

BreakInfo GetBreakInfo() { return CurrentBreak; }

void AckBreak()
{
    if (CurrentBreak.Hit && CurrentBreak.Reason == BreakBreakpoint)
    {
        ResumeBreakpoint = true;
        ResumeCPU = CurrentBreak.CPU;
        ResumePC = CurrentBreak.PC;
    }
    CurrentBreak = {};
    PendingBreak = false;
}

}
