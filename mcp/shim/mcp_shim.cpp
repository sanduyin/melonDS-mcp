/*
 * mcp_shim.cpp — melonDS-mcp fork 的 extern "C" 控制/调试接口层
 *
 * 在 melonDS 核心之上提供扁平 C API，供 Python ctypes 调用。
 * 单一全局 NDS 实例 + SoftRenderer 无头运行。
 *
 * 相对上游 MelonMCP shim 的扩展：
 *  - 双 CPU（ARM9/ARM7）内存读写
 *  - CPU 寄存器读取/写入（含分组寄存器）
 *  - 指令断点、数据观察点、单步执行、指令追踪（基于 src/MCPDebug）
 *  - 运行状态/ROM 信息查询
 *
 * Copyright (C) 2026 melonDS-mcp contributors
 * Licensed under GPLv3 (same as melonDS)
 */

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>
#include <memory>
#include <fstream>
#include <filesystem>

#include "NDS.h"
#include "NDSCart.h"
#include "GPU.h"
#include "GPU_Soft.h"
#include "SPU.h"
#include "Savestate.h"
#include "Args.h"
#include "Platform.h"
#include "MCPDebug.h"

using namespace melonDS;

// ── 全局状态 ──

static NDS* g_nds = nullptr;
static bool g_running = false;
std::string g_save_path;          // melonds_open 设置，Platform::WriteNDSSave 使用
static std::string g_slot_prefix; // 基于 slot 的 savestate 前缀

// ── 工具函数 ──

static std::vector<u8> read_file(const char* path)
{
    std::ifstream f(path, std::ios::binary | std::ios::ate);
    if (!f) return {};
    auto size = f.tellg();
    f.seekg(0);
    std::vector<u8> buf(size);
    f.read(reinterpret_cast<char*>(buf.data()), size);
    return buf;
}

static bool write_file(const char* path, const void* data, size_t len)
{
    std::ofstream f(path, std::ios::binary);
    if (!f) return false;
    f.write(reinterpret_cast<const char*>(data), len);
    return f.good();
}

static std::string slot_path(int index)
{
    return g_slot_prefix + ".slot" + std::to_string(index) + ".mst";
}

static ARM* get_cpu(int cpu)
{
    if (!g_nds) return nullptr;
    return cpu == 1 ? static_cast<ARM*>(&g_nds->ARM7) : static_cast<ARM*>(&g_nds->ARM9);
}

extern "C" {

// ═══════════════════════════════════════════
// 生命周期
// ═══════════════════════════════════════════

int melonds_init(void)
{
    if (g_nds) return 0;

    try {
        NDSArgs args {};
        // 默认：FreeBIOS、JIT 启用、生成固件
        g_nds = new NDS(std::move(args));
        g_nds->Reset();

        // 无头模式下使用软件渲染器以直接访问帧缓冲
        auto renderer = std::make_unique<SoftRenderer>(*g_nds);
        g_nds->SetRenderer(std::move(renderer));

        g_nds->SPU.InitOutput();
        MCPDebug::ResetRuntimeState();
        g_running = false;
        return 0;
    } catch (...) {
        return -1;
    }
}

void melonds_free(void)
{
    if (g_nds) {
        g_nds->Stop();
        delete g_nds;
        g_nds = nullptr;
    }
    g_running = false;
    g_save_path.clear();
    g_slot_prefix.clear();
}

int melonds_open(const char* filename)
{
    if (!g_nds || !filename) return 0;

    auto romdata = read_file(filename);
    if (romdata.empty()) {
        fprintf(stderr, "mcp_shim: 无法读取 ROM: %s\n", filename);
        return 0;
    }

    auto cart = NDSCart::ParseROM(romdata.data(), (u32)romdata.size());
    if (!cart) {
        fprintf(stderr, "mcp_shim: 无法解析 ROM: %s\n", filename);
        return 0;
    }

    // 从 ROM 路径派生存档路径
    std::filesystem::path rom_path(filename);
    g_save_path = (rom_path.parent_path() / rom_path.stem()).string() + ".sav";
    g_slot_prefix = (rom_path.parent_path() / rom_path.stem()).string();

    // 若存档文件存在则载入
    std::optional<NDSCart::NDSCartArgs> cart_args;
    if (std::filesystem::exists(g_save_path)) {
        auto sav = read_file(g_save_path.c_str());
        if (!sav.empty()) {
            NDSCart::NDSCartArgs ca;
            ca.SRAM = std::make_unique<u8[]>(sav.size());
            memcpy(ca.SRAM.get(), sav.data(), sav.size());
            ca.SRAMLength = (u32)sav.size();
            cart_args = std::move(ca);

            cart = NDSCart::ParseROM(romdata.data(), (u32)romdata.size(),
                                     nullptr, std::move(cart_args));
            if (!cart) {
                fprintf(stderr, "mcp_shim: 无法解析带存档的 ROM: %s\n", filename);
                return 0;
            }
        }
    }

    // 插卡并启动
    g_nds->SetNDSCart(std::move(cart));
    g_nds->Reset();

    std::string romname = rom_path.filename().string();
    if (g_nds->NeedsDirectBoot()) {
        g_nds->SetupDirectBoot(romname);
    }

    MCPDebug::ResetRuntimeState();
    g_nds->Start();
    g_running = true;
    return 1;
}

void melonds_pause(void)
{
    g_running = false;
}

void melonds_resume(void)
{
    if (!g_nds) return;
    g_running = true;
    g_nds->Start();
}

void melonds_reset(void)
{
    if (!g_nds) return;
    g_nds->Reset();
    if (g_nds->NeedsDirectBoot()) {
        g_nds->SetupDirectBoot("");
    }
    MCPDebug::ResetRuntimeState();
    melonds_resume();
}

int melonds_running(void)
{
    return g_running ? 1 : 0;
}

// 推进一帧。返回值：
// 0 = 正常完成；1 = 帧内触发调试暂停（断点/观察点/单步完成）
int melonds_cycle(void)
{
    if (!g_nds || !g_running) return 0;
    g_nds->RunFrame();
    if (MCPDebug::GetBreakInfo().Hit)
        return 1;
    return 0;
}

// ═══════════════════════════════════════════
// 显示（截图）
// 输出：RGB24，256x384（上屏+下屏），共 294912 字节
// ═══════════════════════════════════════════

void melonds_screenshot(char* screenshot_buffer)
{
    if (!g_nds || !screenshot_buffer) return;

    void* top_ptr = nullptr;
    void* bot_ptr = nullptr;
    bool ok = g_nds->GPU.GetFramebuffers(&top_ptr, &bot_ptr);
    if (!ok || !top_ptr || !bot_ptr) {
        memset(screenshot_buffer, 0, 256 * 384 * 3);
        return;
    }

    u32* top = (u32*)top_ptr;
    u32* bot = (u32*)bot_ptr;

    // SoftRenderer 输出 BGRA：byte0=B, byte1=G, byte2=R, byte3=A，转为 RGB24
    unsigned char* out = (unsigned char*)screenshot_buffer;
    for (int i = 0; i < 256 * 192; i++) {
        u32 px = top[i];
        out[i * 3 + 0] = (px >> 16) & 0xFF; // R
        out[i * 3 + 1] = (px >> 8) & 0xFF;  // G
        out[i * 3 + 2] = px & 0xFF;         // B
    }
    int off = 256 * 192 * 3;
    for (int i = 0; i < 256 * 192; i++) {
        u32 px = bot[i];
        out[off + i * 3 + 0] = (px >> 16) & 0xFF; // R
        out[off + i * 3 + 1] = (px >> 8) & 0xFF;  // G
        out[off + i * 3 + 2] = px & 0xFF;         // B
    }
}

// ═══════════════════════════════════════════
// 输入
// 外部约定：1 = 按下；melonDS 硬件约定：1 = 释放（KEYINPUT）
// ═══════════════════════════════════════════

void melonds_input_keypad_update(unsigned short keys)
{
    if (!g_nds) return;
    // 反相：外部 1=按下 -> melonDS 1=释放
    u32 mask = (~(u32)keys) & 0xFFF;
    g_nds->SetKeyMask(mask);
}

unsigned short melonds_input_keypad_get(void)
{
    if (!g_nds) return 0;
    // KeyInput: bit0-9 = 标准按键（1=释放），bit16-17 = X/Y（1=释放）
    u32 ki = g_nds->KeyInput;
    u32 lo = ki & 0x3FF;
    u32 hi = (ki >> 16) & 0x3;
    u32 mask = lo | (hi << 10);
    return (unsigned short)((~mask) & 0xFFF);
}

void melonds_input_set_touch_pos(unsigned short x, unsigned short y)
{
    if (!g_nds) return;
    g_nds->TouchScreen(x, y);
}

void melonds_input_release_touch(void)
{
    if (!g_nds) return;
    g_nds->ReleaseScreen();
}

void melonds_set_lid_closed(int closed)
{
    if (!g_nds) return;
    g_nds->SetLidClosed(closed != 0);
}

int melonds_get_lid_closed(void)
{
    if (!g_nds) return 0;
    return g_nds->IsLidClosed() ? 1 : 0;
}

// ═══════════════════════════════════════════
// Savestate
// ═══════════════════════════════════════════

int melonds_savestate_save(const char* filename)
{
    if (!g_nds || !filename) return 0;

    Savestate state;
    g_nds->DoSavestate(&state);
    state.Finish();

    if (state.Error) return 0;

    return write_file(filename, state.Buffer(), state.Length()) ? 1 : 0;
}

int melonds_savestate_load(const char* filename)
{
    if (!g_nds || !filename) return 0;

    auto buf = read_file(filename);
    if (buf.empty()) return 0;

    Savestate state(buf.data(), (u32)buf.size(), false);
    g_nds->DoSavestate(&state);

    if (state.Error) return 0;

    MCPDebug::ResetRuntimeState();
    return 1;
}

void melonds_savestate_slot_save(int index)
{
    if (g_slot_prefix.empty()) return;
    melonds_savestate_save(slot_path(index).c_str());
}

void melonds_savestate_slot_load(int index)
{
    if (g_slot_prefix.empty()) return;
    melonds_savestate_load(slot_path(index).c_str());
}

int melonds_savestate_slot_exists(int index)
{
    if (g_slot_prefix.empty()) return 0;
    return std::filesystem::exists(slot_path(index)) ? 1 : 0;
}

// ═══════════════════════════════════════════
// 内存（cpu: 0=ARM9, 1=ARM7）
// ═══════════════════════════════════════════

unsigned char melonds_memory_read8(int cpu, unsigned int address)
{
    if (!g_nds) return 0;
    return cpu == 1 ? g_nds->ARM7Read8(address) : g_nds->ARM9Read8(address);
}

unsigned short melonds_memory_read16(int cpu, unsigned int address)
{
    if (!g_nds) return 0;
    return cpu == 1 ? g_nds->ARM7Read16(address) : g_nds->ARM9Read16(address);
}

unsigned int melonds_memory_read32(int cpu, unsigned int address)
{
    if (!g_nds) return 0;
    return cpu == 1 ? g_nds->ARM7Read32(address) : g_nds->ARM9Read32(address);
}

int melonds_memory_read_block(int cpu, unsigned int address, int size, unsigned char* buffer)
{
    if (!g_nds || !buffer || size <= 0) return 0;
    if (cpu == 1) {
        for (int i = 0; i < size; i++)
            buffer[i] = g_nds->ARM7Read8(address + i);
    } else {
        for (int i = 0; i < size; i++)
            buffer[i] = g_nds->ARM9Read8(address + i);
    }
    return size;
}

void melonds_memory_write8(int cpu, unsigned int address, unsigned char value)
{
    if (!g_nds) return;
    if (cpu == 1) g_nds->ARM7Write8(address, value);
    else g_nds->ARM9Write8(address, value);
}

void melonds_memory_write16(int cpu, unsigned int address, unsigned short value)
{
    if (!g_nds) return;
    if (cpu == 1) g_nds->ARM7Write16(address, value);
    else g_nds->ARM9Write16(address, value);
}

void melonds_memory_write32(int cpu, unsigned int address, unsigned int value)
{
    if (!g_nds) return;
    if (cpu == 1) g_nds->ARM7Write32(address, value);
    else g_nds->ARM9Write32(address, value);
}

int melonds_memory_write_block(int cpu, unsigned int address, int size, const unsigned char* buffer)
{
    if (!g_nds || !buffer || size <= 0) return 0;
    if (cpu == 1) {
        for (int i = 0; i < size; i++)
            g_nds->ARM7Write8(address + i, buffer[i]);
    } else {
        for (int i = 0; i < size; i++)
            g_nds->ARM9Write8(address + i, buffer[i]);
    }
    return size;
}

// ═══════════════════════════════════════════
// 调试：寄存器
// ═══════════════════════════════════════════

// 输出布局（48 个 u32）：
// [0..15]  R0-R15（R15=PC）
// [16]     CPSR
// [17]     Cycles（未提交周期计数）
// [18]     Halted
// [19]     IRQ
// [20]     CodeRegion
// [21]     DataRegion
// [22..29] R_FIQ[0..7]（R8-R14 + SPSR_fiq）
// [30..32] R_SVC[0..2]（R13-R14 + SPSR_svc）
// [33..35] R_ABT[0..2]
// [36..38] R_IRQ[0..2]
// [39..41] R_UND[0..2]
// [42..47] 保留
void melonds_debug_get_registers(int cpu, unsigned int* out)
{
    ARM* arm = get_cpu(cpu);
    if (!arm || !out) return;

    for (int i = 0; i < 16; i++) out[i] = arm->R[i];
    out[16] = arm->CPSR;
    out[17] = (u32)arm->Cycles;
    out[18] = arm->Halted;
    out[19] = arm->IRQ;
    out[20] = arm->CodeRegion;
    out[21] = arm->DataRegion;
    for (int i = 0; i < 8; i++) out[22 + i] = arm->R_FIQ[i];
    for (int i = 0; i < 3; i++) out[30 + i] = arm->R_SVC[i];
    for (int i = 0; i < 3; i++) out[33 + i] = arm->R_ABT[i];
    for (int i = 0; i < 3; i++) out[36 + i] = arm->R_IRQ[i];
    for (int i = 0; i < 3; i++) out[39 + i] = arm->R_UND[i];
    for (int i = 42; i < 48; i++) out[i] = 0;
}

// index: 0-15 = R0-R15，16 = CPSR
int melonds_debug_write_register(int cpu, int index, unsigned int value)
{
    ARM* arm = get_cpu(cpu);
    if (!arm) return 0;
    if (index < 0 || index > 16) return 0;

    if (index < 16) arm->R[index] = value;
    else arm->CPSR = value;
    return 1;
}

unsigned int melonds_get_pc(int cpu)
{
    if (!g_nds) return 0;
    return g_nds->GetPC(cpu == 1 ? 1 : 0);
}

// ═══════════════════════════════════════════
// 调试：断点
// ═══════════════════════════════════════════

int melonds_debug_bp_add(int cpu, unsigned int address)
{
    return MCPDebug::AddBreakpoint(cpu == 1 ? 1 : 0, address);
}

int melonds_debug_bp_remove(int bp_id)
{
    return MCPDebug::RemoveBreakpoint(bp_id) ? 1 : 0;
}

int melonds_debug_bp_set_enabled(int bp_id, int enabled)
{
    return MCPDebug::SetBreakpointEnabled(bp_id, enabled != 0) ? 1 : 0;
}

// cpu: 0=ARM9 1=ARM7 -1=全部
void melonds_debug_bp_clear(int cpu)
{
    MCPDebug::ClearBreakpoints(cpu < 0 ? 0xFFFFFFFF : (u32)cpu);
}

// 拷贝断点表到平行数组，返回条数
int melonds_debug_bp_list(int cpu, int* out_ids, unsigned int* out_addrs,
                          unsigned char* out_cpus, unsigned char* out_enabled, int max)
{
    auto bps = MCPDebug::GetBreakpoints();
    int n = 0;
    for (const auto& bp : bps) {
        if (n >= max) break;
        if (cpu >= 0 && (int)bp.CPU != cpu) continue;
        if (out_ids) out_ids[n] = bp.ID;
        if (out_addrs) out_addrs[n] = bp.Addr;
        if (out_cpus) out_cpus[n] = (unsigned char)bp.CPU;
        if (out_enabled) out_enabled[n] = bp.Enabled ? 1 : 0;
        n++;
    }
    return n;
}

// ═══════════════════════════════════════════
// 调试：观察点
// ═══════════════════════════════════════════

// kind: 1=读 2=写 3=读写
int melonds_debug_wp_add(int cpu, unsigned int address, unsigned int size, int kind)
{
    return MCPDebug::AddWatchpoint(cpu == 1 ? 1 : 0, address, size, (u8)(kind & 3));
}

int melonds_debug_wp_remove(int wp_id)
{
    return MCPDebug::RemoveWatchpoint(wp_id) ? 1 : 0;
}

int melonds_debug_wp_set_enabled(int wp_id, int enabled)
{
    return MCPDebug::SetWatchpointEnabled(wp_id, enabled != 0) ? 1 : 0;
}

void melonds_debug_wp_clear(int cpu)
{
    MCPDebug::ClearWatchpoints(cpu < 0 ? 0xFFFFFFFF : (u32)cpu);
}

int melonds_debug_wp_list(int cpu, int* out_ids, unsigned int* out_starts,
                          unsigned int* out_ends, unsigned char* out_cpus,
                          unsigned char* out_kinds, unsigned char* out_enabled, int max)
{
    auto wps = MCPDebug::GetWatchpoints();
    int n = 0;
    for (const auto& wp : wps) {
        if (n >= max) break;
        if (cpu >= 0 && (int)wp.CPU != cpu) continue;
        if (out_ids) out_ids[n] = wp.ID;
        if (out_starts) out_starts[n] = wp.AddrStart;
        if (out_ends) out_ends[n] = wp.AddrEnd;
        if (out_cpus) out_cpus[n] = (unsigned char)wp.CPU;
        if (out_kinds) out_kinds[n] = wp.Kind;
        if (out_enabled) out_enabled[n] = wp.Enabled ? 1 : 0;
        n++;
    }
    return n;
}

// ═══════════════════════════════════════════
// 调试：单步
// ═══════════════════════════════════════════

void melonds_debug_step_request(int cpu, unsigned int count)
{
    MCPDebug::RequestStep(cpu == 1 ? 1 : 0, count);
}

int melonds_debug_step_pending(void)
{
    return MCPDebug::StepPending() ? 1 : 0;
}

// ═══════════════════════════════════════════
// 调试：指令追踪
// ═══════════════════════════════════════════

// cpuMask: bit0=ARM9 bit1=ARM7
void melonds_debug_trace_start(int cpu_mask, unsigned int addr_start, unsigned int addr_end)
{
    MCPDebug::TraceStart((u32)cpu_mask & 3, addr_start, addr_end);
}

void melonds_debug_trace_stop(void)
{
    MCPDebug::TraceStop();
}

int melonds_debug_trace_active(void)
{
    return MCPDebug::TraceActive() ? 1 : 0;
}

unsigned int melonds_debug_trace_count(void)
{
    return MCPDebug::TraceCount();
}

// TraceEntryC 布局：{u32 cpu; u32 pc; u32 instr; u32 cpsr;}
unsigned int melonds_debug_trace_drain(unsigned int* out_data, unsigned int max_entries)
{
    if (!out_data) return 0;
    return MCPDebug::DrainTrace(reinterpret_cast<MCPDebug::TraceEntry*>(out_data), max_entries);
}

// ═══════════════════════════════════════════
// 调试：命中状态与观察点事件
// ═══════════════════════════════════════════

// out[0]=hit out[1]=cpu out[2]=reason(1=bp 2=wp 3=step) out[3]=id
// out[4]=pc out[5]=addr
void melonds_debug_break_info(unsigned int* out)
{
    if (!out) return;
    auto bi = MCPDebug::GetBreakInfo();
    out[0] = bi.Hit ? 1 : 0;
    out[1] = bi.CPU;
    out[2] = bi.Reason;
    out[3] = (u32)bi.ID;
    out[4] = bi.PC;
    out[5] = bi.Addr;
}

void melonds_debug_break_ack(void)
{
    MCPDebug::AckBreak();
}

// WatchEventC 布局：{u32 cpu; u32 addr; u32 pc; u32 kind; u32 size; u32 value;}
// out_data 为 6*u32 平铺数组
unsigned int melonds_debug_wp_events(unsigned int* out_data, unsigned int max_events)
{
    if (!out_data) return 0;
    MCPDebug::WatchEvent evts[256];
    u32 n = MCPDebug::DrainWatchEvents(evts, max_events < 256 ? max_events : 256);
    for (u32 i = 0; i < n; i++) {
        u32* row = out_data + i * 6;
        row[0] = evts[i].CPU;
        row[1] = evts[i].Addr;
        row[2] = evts[i].PC;
        row[3] = evts[i].Kind;
        row[4] = evts[i].Size;
        row[5] = evts[i].Value;
    }
    return n;
}

// 指令级调试功能是否激活（决定是否需要关闭 JIT）
int melonds_debug_hooks_active(void)
{
    return MCPDebug::AnyHooksActive() ? 1 : 0;
}

int melonds_debug_data_hooks_active(void)
{
    return MCPDebug::DataHooksActive() ? 1 : 0;
}

// ═══════════════════════════════════════════
// 状态查询
// ═══════════════════════════════════════════

// out[0]=running out[1]=frames out[2]=lag_frames
// out[3]=jit_enabled out[4]=console_type out[5]=rom_inserted
// out[6]=pc9 out[7]=pc7 out[8]=num_cpus(2)
void melonds_get_status(unsigned int* out)
{
    if (!out) return;
    memset(out, 0, 9 * sizeof(u32));
    if (!g_nds) return;

    out[0] = g_running ? 1 : 0;
    out[1] = g_nds->NumFrames;
    out[2] = g_nds->NumLagFrames;
    out[3] = g_nds->IsJITEnabled() ? 1 : 0;
    out[4] = (u32)g_nds->ConsoleType;
    out[5] = g_nds->CartInserted() ? 1 : 0;
    out[6] = g_nds->GetPC(0);
    out[7] = g_nds->GetPC(1);
    out[8] = 2;
}

// 系统时钟周期（模拟运行时长度量）
unsigned long long melonds_get_cycles(int num)
{
    if (!g_nds) return 0;
    return g_nds->GetSysClockCycles(num);
}

// ROM 信息。title/code/maker 为输出缓冲（各至少 16 字节）
int melonds_get_rom_info(char* title, char* code, char* maker,
                         unsigned int* out_sizes)
{
    if (!g_nds || !g_nds->CartInserted()) return 0;

    const NDSHeader& hdr = g_nds->GetNDSCart()->GetHeader();

    if (title) {
        char t[13];
        memcpy(t, hdr.GameTitle, 12);
        t[12] = 0;
        strcpy(title, t);
    }
    if (code) {
        char c[5];
        memcpy(c, hdr.GameCode, 4);
        c[4] = 0;
        strcpy(code, c);
    }
    if (maker) {
        char m[3];
        memcpy(m, hdr.MakerCode, 2);
        m[2] = 0;
        strcpy(maker, m);
    }
    if (out_sizes) {
        // [0]=ROMSize [1]=ARM9RAMAddress [2]=ARM9Entry [3]=ARM7RAMAddress [4]=ARM7Entry [5]=BannerOffset
        out_sizes[0] = hdr.ROMSize;
        out_sizes[1] = hdr.ARM9RAMAddress;
        out_sizes[2] = hdr.ARM9EntryAddress;
        out_sizes[3] = hdr.ARM7RAMAddress;
        out_sizes[4] = hdr.ARM7EntryAddress;
        out_sizes[5] = hdr.BannerOffset;
    }
    return 1;
}

// ═══════════════════════════════════════════
// 音频
// ═══════════════════════════════════════════

void melonds_audio_enable(void)
{
    if (!g_nds) return;
    g_nds->SPU.InitOutput();
}

void melonds_audio_disable(void)
{
    if (!g_nds) return;
    g_nds->SPU.DrainOutput();
}

unsigned int melonds_audio_samples_available(void)
{
    if (!g_nds) return 0;
    return (unsigned int)g_nds->SPU.GetOutputSize();
}

unsigned int melonds_audio_read(signed short* output, unsigned int max_frames)
{
    if (!g_nds || !output) return 0;
    int read = g_nds->SPU.ReadOutput(output, (int)max_frames);
    return (unsigned int)(read > 0 ? read : 0);
}

// ═══════════════════════════════════════════
// 存档（电池备份）
// ═══════════════════════════════════════════

int melonds_backup_import(const char* filename)
{
    if (!g_nds || !filename) return 0;

    auto sav = read_file(filename);
    if (sav.empty()) return 0;

    g_nds->SetNDSSave(sav.data(), (u32)sav.size());
    return 1;
}

int melonds_backup_export(const char* filename)
{
    if (!g_nds || !filename) return 0;

    const u8* data = g_nds->GetNDSSave();
    u32 len = g_nds->GetNDSSaveLength();
    if (!data || len == 0) return 0;

    return write_file(filename, data, len) ? 1 : 0;
}

// ═══════════════════════════════════════════
// 渲染跳过 / JIT
// ═══════════════════════════════════════════

void melonds_set_skip_render(int skip)
{
    if (!g_nds) return;
    g_nds->GPU.SkipRender = (skip != 0);
}

int melonds_get_skip_render(void)
{
    if (!g_nds) return 0;
    return g_nds->GPU.SkipRender ? 1 : 0;
}

int melonds_jit_enabled(void)
{
    if (!g_nds) return 0;
    return g_nds->IsJITEnabled() ? 1 : 0;
}

int melonds_set_jit(int enabled)
{
    if (!g_nds) return 0;
#ifdef JIT_ENABLED
    if (enabled)
        g_nds->SetJITArgs(JITArgs {});
    else
        g_nds->SetJITArgs(std::nullopt);
    return 1;
#else
    (void)enabled;
    return 0;
#endif
}

} // extern "C"
