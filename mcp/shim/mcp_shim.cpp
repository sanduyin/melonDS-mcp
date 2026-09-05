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
 * Source: https://github.com/sanduyin/melonDS-mcp
 */

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>
#include <memory>
#include <fstream>
#include <filesystem>
#include <limits>

#include "NDS.h"
#include "NDSCart.h"
#include "GPU.h"
#include "GPU_Soft.h"
#include "SPU.h"
#include "Savestate.h"
#include "Args.h"
#include "Platform.h"
#include "MCPDebug.h"
#include "mcp_export.h"

using namespace melonDS;

// ── 全局状态 ──

static NDS* g_nds = nullptr;
static bool g_running = false;
std::string g_save_path;          // melonds_open 设置，Platform::WriteNDSSave 使用
static std::string g_slot_prefix; // 基于 slot 的 savestate 前缀

// ── 工具函数 ──

static std::string path_utf8(const std::filesystem::path& path)
{
    const auto value = path.u8string();
    return {reinterpret_cast<const char*>(value.data()), value.size()};
}

static std::vector<u8> read_file(const char* path)
{
    std::ifstream f(std::filesystem::u8path(path), std::ios::binary | std::ios::ate);
    if (!f) return {};
    auto size = f.tellg();
    if (size <= 0 || size > std::numeric_limits<u32>::max()) return {};
    f.seekg(0);
    std::vector<u8> buf(size);
    f.read(reinterpret_cast<char*>(buf.data()), size);
    if (!f) return {};
    return buf;
}

static bool write_file(const char* path, const void* data, size_t len)
{
    std::ofstream f(std::filesystem::u8path(path), std::ios::binary);
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
    if (!g_nds || cpu < 0 || cpu > 1) return nullptr;
    return cpu == 1 ? static_cast<ARM*>(&g_nds->ARM7) : static_cast<ARM*>(&g_nds->ARM9);
}

// Resolve only ordinary backing storage, never a bus or CPU data access. This
// uses enabled ITCM first; the data view then uses DTCM, while the instruction
// view ignores DTCM, matching CP15.cpp. BIOS reads intentionally inspect the stored
// image, not ARM7's PC-dependent read protection or the MPU permission state.
static const u8* debug_mapped_byte(const NDS& nds, int cpu, u32 address,
                                  bool instruction_view, int* region = nullptr)
{
    const auto mapped = [region](const u8* pointer, int memory_region) {
        if (region) *region = memory_region;
        return pointer;
    };
    if (cpu == 0)
    {
        const auto& arm9 = nds.ARM9;
        if (address < arm9.ITCMSize)
            return mapped(&arm9.ITCM[address & (ITCMPhysicalSize - 1)], ARMJIT_Memory::memregion_ITCM);
        if (!instruction_view && (address & arm9.DTCMMask) == arm9.DTCMBase)
            return mapped(&arm9.DTCM[address & (DTCMPhysicalSize - 1)], ARMJIT_Memory::memregion_DTCM);

        if ((address & 0xFF000000) == 0x02000000)
            return mapped(&nds.MainRAM[address & nds.MainRAMMask], ARMJIT_Memory::memregion_MainRAM);
        if ((address & 0xFF000000) == 0x03000000 && nds.SWRAM_ARM9.Mem)
            return mapped(&nds.SWRAM_ARM9.Mem[address & nds.SWRAM_ARM9.Mask], ARMJIT_Memory::memregion_SharedWRAM);
        if ((address & 0xFFFFF000) == 0xFFFF0000)
            return mapped(&nds.GetARM9BIOS()[address & 0xFFF], ARMJIT_Memory::memregion_BIOS9);
    }
    else
    {
        if (address < nds.GetARM7BIOS().size())
            return mapped(&nds.GetARM7BIOS()[address], ARMJIT_Memory::memregion_BIOS7);
        if ((address & 0xFF000000) == 0x02000000)
            return mapped(&nds.MainRAM[address & nds.MainRAMMask], ARMJIT_Memory::memregion_MainRAM);
        if ((address & 0xFF800000) == 0x03000000)
        {
            if (nds.SWRAM_ARM7.Mem)
                return mapped(&nds.SWRAM_ARM7.Mem[address & nds.SWRAM_ARM7.Mask], ARMJIT_Memory::memregion_SharedWRAM);
            return mapped(&nds.ARM7WRAM[address & (nds.ARM7WRAMSize - 1)], ARMJIT_Memory::memregion_WRAM7);
        }
        if ((address & 0xFF800000) == 0x03800000)
            return mapped(&nds.ARM7WRAM[address & (nds.ARM7WRAMSize - 1)], ARMJIT_Memory::memregion_WRAM7);
    }
    return nullptr;
}

static u32 peek_mapped_block(int cpu, u32 address, u32 length, u8* dest, bool code)
{
    if (!g_nds || g_nds->ConsoleType != 0 || !dest || cpu < 0 || cpu > 1
        || length == 0 || length > 4096
        || static_cast<u64>(address) + length > 0x100000000ULL)
        return 0;
    u8 snapshot[4096];
    for (u32 index = 0; index < length; ++index)
    {
        const u8* source = debug_mapped_byte(*g_nds, cpu, address + index, code);
        if (!source) return 0;
        snapshot[index] = *source;
    }
    std::memcpy(dest, snapshot, length);
    return length;
}

// Match physical byte identities, not virtual addresses: either CPU may have
// prefetched the same RAM through another mirror or shared-WRAM mapping.
// Update only affected bytes, retaining previously fetched values for any
// adjacent unsupported/device address. Never refetch through a bus callback.
static void patch_prefetch_slot(NDS& nds, ARM& arm, int slot, u32 address, u32 size,
                                u8* const* targets, u32 length)
{
    for (u32 byte = 0; byte < size; ++byte)
    {
        const u8* source = debug_mapped_byte(nds, arm.Num, address + byte, true);
        if (!source) continue;
        for (u32 index = 0; index < length; ++index)
        {
            if (source != targets[index]) continue;
            const u32 shift = byte * 8;
            arm.NextInstr[slot] = (arm.NextInstr[slot] & ~(0xFFu << shift))
                | (static_cast<u32>(*source) << shift);
            break;
        }
    }
}

static void refresh_patched_prefetch(NDS& nds, ARM& arm, u8* const* targets, u32 length)
{
    const bool thumb = (arm.CPSR & 0x20) != 0;
    const u32 pc = arm.R[15] - (thumb ? 2 : 4);
    if (!thumb)
    {
        patch_prefetch_slot(nds, arm, 0, pc, 4, targets, length);
        patch_prefetch_slot(nds, arm, 1, pc + 4, 4, targets, length);
    }
    else if (arm.Num == 1)
    {
        patch_prefetch_slot(nds, arm, 0, pc, 2, targets, length);
        patch_prefetch_slot(nds, arm, 1, pc + 2, 2, targets, length);
    }
    else
    {
        // ARM9 fetches Thumb instructions as words (ARM.cpp::FillPipeline).
        // At word alignment the next halfword occurs in BOTH cached slots.
        patch_prefetch_slot(nds, arm, 0, pc, (pc & 2) ? 2 : 4, targets, length);
        patch_prefetch_slot(nds, arm, 1, pc + 2, (pc & 2) ? 4 : 2, targets, length);
    }
}

static void invalidate_poked_byte(NDS& nds, int cpu, int region, u32 address)
{
    switch (region)
    {
    case ARMJIT_Memory::memregion_ITCM:
        nds.JIT.CheckAndInvalidate<0, ARMJIT_Memory::memregion_ITCM>(address);
        break;
    case ARMJIT_Memory::memregion_MainRAM:
        if (cpu == 0) nds.JIT.CheckAndInvalidate<0, ARMJIT_Memory::memregion_MainRAM>(address);
        else nds.JIT.CheckAndInvalidate<1, ARMJIT_Memory::memregion_MainRAM>(address);
        break;
    case ARMJIT_Memory::memregion_SharedWRAM:
        if (cpu == 0) nds.JIT.CheckAndInvalidate<0, ARMJIT_Memory::memregion_SharedWRAM>(address);
        else nds.JIT.CheckAndInvalidate<1, ARMJIT_Memory::memregion_SharedWRAM>(address);
        break;
    case ARMJIT_Memory::memregion_WRAM7:
        nds.JIT.CheckAndInvalidate<1, ARMJIT_Memory::memregion_WRAM7>(address);
        break;
    }
}

extern "C" {

// ═══════════════════════════════════════════
// 生命周期
// ═══════════════════════════════════════════

MELONDS_MCP_API int melonds_init(void)
{
    if (g_nds) return 0;

    try {
        NDSArgs args {};
        // FreeBIOS and generated firmware; interpreter-first for debugger hooks.
        args.JIT = std::nullopt;
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
        delete g_nds;
        g_nds = nullptr;
        return -1;
    }
}

MELONDS_MCP_API void melonds_free(void)
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

MELONDS_MCP_API int melonds_open(const char* filename)
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
    std::filesystem::path rom_path = std::filesystem::u8path(filename);
    g_save_path = path_utf8(rom_path.parent_path() / rom_path.stem()) + ".sav";
    g_slot_prefix = path_utf8(rom_path.parent_path() / rom_path.stem());

    // 若存档文件存在则载入
    std::optional<NDSCart::NDSCartArgs> cart_args;
    if (std::filesystem::exists(std::filesystem::u8path(g_save_path))) {
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

    std::string romname = path_utf8(rom_path.filename());
    if (g_nds->NeedsDirectBoot()) {
        g_nds->SetupDirectBoot(romname);
    }

    MCPDebug::ResetRuntimeState();
    g_nds->Start();
    g_running = true;
    return 1;
}

MELONDS_MCP_API void melonds_pause(void)
{
    g_running = false;
}

MELONDS_MCP_API void melonds_resume(void)
{
    if (!g_nds) return;
    g_running = true;
    g_nds->Start();
}

MELONDS_MCP_API void melonds_reset(void)
{
    if (!g_nds) return;
    g_nds->Reset();
    if (g_nds->NeedsDirectBoot()) {
        g_nds->SetupDirectBoot("");
    }
    MCPDebug::ResetRuntimeState();
    melonds_resume();
}

MELONDS_MCP_API int melonds_running(void)
{
    return g_running ? 1 : 0;
}

// 推进一帧。返回值：
// 0 = 正常完成；1 = 帧内触发调试暂停（断点/观察点/单步完成）
MELONDS_MCP_API int melonds_cycle(void)
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

MELONDS_MCP_API void melonds_screenshot(char* screenshot_buffer)
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

// Physical GPU resources, independent of CPU mapping / VRAMCNT. These reads
// are snapshots of the software core's storage, not bus/MMIO transactions.
// region 0: VRAM banks A-I; region 1: A/B standard palettes; region 2: A/B OAM.
// Each palette bank includes 0x200 bytes BG + 0x200 bytes OBJ. Each OAM bank
// contains all 128 entries (including the shared affine parameter words).
MELONDS_MCP_API uint32_t melonds_gpu_read(int region, int bank, uint32_t offset,
                                        uint8_t* dest, uint32_t length)
{
    if (!g_nds || !dest || length == 0) return 0;

    const auto& gpu = g_nds->GPU;
    const u8* source = nullptr;
    u32 size = 0;
    switch (region)
    {
    case 0:
        if (bank < 0 || bank >= 9) return 0;
        source = gpu.VRAM[bank];
        size = gpu.VRAMMask[bank] + 1;
        break;
    case 1:
        if (bank < 0 || bank >= 2) return 0;
        source = gpu.Palette + bank * 0x400;
        size = 0x400;
        break;
    case 2:
        if (bank < 0 || bank >= 2) return 0;
        source = gpu.OAM + bank * 0x400;
        size = 0x400;
        break;
    default:
        return 0;
    }

    // Subtraction after the offset check avoids offset+length wrapping u32.
    // Reject the whole request: never return a silently truncated bank range.
    if (offset > size || length > size - offset) return 0;
    std::memcpy(dest, source + offset, length);
    return length;
}

// Fixed ABI: [frame, VCOUNT, DISPCNT_A, DISPCNT_B, POWCNT1,
//             VRAMCNT_A .. VRAMCNT_I, reserved=0, reserved=0].
// Caller capacity is measured in u32 words; only the first 16 are written.
MELONDS_MCP_API int melonds_gpu_state(uint32_t* words, uint32_t capacity)
{
    if (!g_nds || !words || capacity < 16) return 0;

    const auto& gpu = g_nds->GPU;
    const u32 state[16] = {
        g_nds->NumFrames, gpu.VCount, gpu.GPU2D_A.DispCnt, gpu.GPU2D_B.DispCnt,
        g_nds->PowerControl9,
        gpu.VRAMCNT[0], gpu.VRAMCNT[1], gpu.VRAMCNT[2], gpu.VRAMCNT[3],
        gpu.VRAMCNT[4], gpu.VRAMCNT[5], gpu.VRAMCNT[6], gpu.VRAMCNT[7],
        gpu.VRAMCNT[8], 0, 0,
    };
    std::memcpy(words, state, sizeof(state));
    return 16;
}

// ═══════════════════════════════════════════
// 输入
// 外部约定：1 = 按下；melonDS 硬件约定：1 = 释放（KEYINPUT）
// ═══════════════════════════════════════════

MELONDS_MCP_API void melonds_input_keypad_update(unsigned short keys)
{
    if (!g_nds) return;
    // 反相：外部 1=按下 -> melonDS 1=释放
    u32 mask = (~(u32)keys) & 0xFFF;
    g_nds->SetKeyMask(mask);
}

MELONDS_MCP_API unsigned short melonds_input_keypad_get(void)
{
    if (!g_nds) return 0;
    // KeyInput: bit0-9 = 标准按键（1=释放），bit16-17 = X/Y（1=释放）
    u32 ki = g_nds->KeyInput;
    u32 lo = ki & 0x3FF;
    u32 hi = (ki >> 16) & 0x3;
    u32 mask = lo | (hi << 10);
    return (unsigned short)((~mask) & 0xFFF);
}

MELONDS_MCP_API void melonds_input_set_touch_pos(unsigned short x, unsigned short y)
{
    if (!g_nds) return;
    g_nds->TouchScreen(x, y);
}

MELONDS_MCP_API void melonds_input_release_touch(void)
{
    if (!g_nds) return;
    g_nds->ReleaseScreen();
}

MELONDS_MCP_API void melonds_set_lid_closed(int closed)
{
    if (!g_nds) return;
    g_nds->SetLidClosed(closed != 0);
}

MELONDS_MCP_API int melonds_get_lid_closed(void)
{
    if (!g_nds) return 0;
    return g_nds->IsLidClosed() ? 1 : 0;
}

// ═══════════════════════════════════════════
// Savestate
// ═══════════════════════════════════════════

MELONDS_MCP_API int melonds_savestate_save(const char* filename)
{
    if (!g_nds || !filename) return 0;

    Savestate state;
    g_nds->DoSavestate(&state);
    state.Finish();

    if (state.Error) return 0;

    return write_file(filename, state.Buffer(), state.Length()) ? 1 : 0;
}

MELONDS_MCP_API int melonds_savestate_load(const char* filename)
{
    if (!g_nds || !filename) return 0;

    auto buf = read_file(filename);
    if (buf.empty()) return 0;

    Savestate state(buf.data(), (u32)buf.size(), false);
    // A rejected header must not enter component loaders: some loaders have
    // post-load side effects even after the stream has already reported Error.
    if (state.Error) return 0;
    g_nds->DoSavestate(&state);

    if (state.Error) return 0;

    MCPDebug::ResetRuntimeState();
    return 1;
}

MELONDS_MCP_API void melonds_savestate_slot_save(int index)
{
    if (g_slot_prefix.empty()) return;
    melonds_savestate_save(slot_path(index).c_str());
}

MELONDS_MCP_API void melonds_savestate_slot_load(int index)
{
    if (g_slot_prefix.empty()) return;
    melonds_savestate_load(slot_path(index).c_str());
}

MELONDS_MCP_API int melonds_savestate_slot_exists(int index)
{
    if (g_slot_prefix.empty()) return 0;
    return std::filesystem::exists(std::filesystem::u8path(slot_path(index))) ? 1 : 0;
}

// ═══════════════════════════════════════════
// 内存（cpu: 0=ARM9, 1=ARM7）
// ═══════════════════════════════════════════

// Side-effect-free DS mapped-data inspection (1..4096 bytes). Unlike the
// existing bus-read API this includes ARM9 TCM and rejects MMIO, GPU memory,
// cartridges, unmapped bytes, and DSi. It neither executes CPU access checks
// nor changes cycles, watchpoints, device FIFOs, or memory mappings.
// This is NOT the ARM9 instruction-fetch view: code fetch does not use DTCM.
MELONDS_MCP_API uint32_t melonds_memory_peek_block(int cpu, uint32_t address,
                                                 uint32_t length, uint8_t* dest)
{
    return peek_mapped_block(cpu, address, length, dest, false);
}

// Instruction BACKING view, not a claim about current I-cache/prefetch bytes.
// ARM9 uses ITCM but never DTCM for instruction fetch; the two views can differ.
MELONDS_MCP_API uint32_t melonds_code_peek_block(int cpu, uint32_t address,
                                               uint32_t length, uint8_t* dest)
{
    return peek_mapped_block(cpu, address, length, dest, true);
}

// view=0: mapped data backing write, preserving all prefetched instructions.
// view=1: instruction backing patch, also making affected prefetched code on
// BOTH CPUs immediately coherent. BIOS/device/GPU writes are never accepted.
MELONDS_MCP_API uint32_t melonds_memory_poke_block(int cpu, uint32_t address,
                                                 uint32_t length, const uint8_t* source,
                                                 int view)
{
    if (!g_nds || g_nds->ConsoleType != 0 || !source || cpu < 0 || cpu > 1
        || (view != 0 && view != 1) || length == 0 || length > 4096
        || static_cast<u64>(address) + length > 0x100000000ULL)
        return 0;

    u8* targets[4096];
    int regions[4096];
    u8 bytes[4096];
    bool touchesDTCM = false;
    for (u32 index = 0; index < length; ++index)
    {
        const u8* target = debug_mapped_byte(*g_nds, cpu, address + index, view == 1,
                                            &regions[index]);
        if (!target || regions[index] == ARMJIT_Memory::memregion_BIOS9
            || regions[index] == ARMJIT_Memory::memregion_BIOS7)
            return 0;
        // All remaining resolver branches refer to mutable RAM/WRAM/TCM.
        targets[index] = const_cast<u8*>(target);
        touchesDTCM |= regions[index] == ARMJIT_Memory::memregion_DTCM;
    }
    std::memcpy(bytes, source, length); // Also safe if the caller's input aliases a target.

    // Retire host-compiled code/literals BEFORE changing protected RAM pages.
    // DTCM has no code-index region: a PC-relative JIT literal may still read
    // that data overlay, so invalidate the whole host block cache in this case.
    if (touchesDTCM)
    {
        // Reset also writes the host code arena (W^X on Apple ARM64/NetBSD).
        g_nds->JIT.JitEnableWrite();
        g_nds->JIT.ResetBlockCache();
        g_nds->JIT.JitEnableExecute();
    }
    else for (u32 index = 0; index < length; ++index)
        invalidate_poked_byte(*g_nds, cpu, regions[index], address + index);

    for (u32 index = 0; index < length; ++index)
        *targets[index] = bytes[index];
    if (view == 1)
    {
        g_nds->ARM9.ICacheInvalidateAll();
        refresh_patched_prefetch(*g_nds, g_nds->ARM9, targets, length);
        refresh_patched_prefetch(*g_nds, g_nds->ARM7, targets, length);
    }
    return length;
}

MELONDS_MCP_API unsigned char melonds_memory_read8(int cpu, unsigned int address)
{
    if (!g_nds) return 0;
    return cpu == 1 ? g_nds->ARM7Read8(address) : g_nds->ARM9Read8(address);
}

MELONDS_MCP_API unsigned short melonds_memory_read16(int cpu, unsigned int address)
{
    if (!g_nds) return 0;
    return cpu == 1 ? g_nds->ARM7Read16(address) : g_nds->ARM9Read16(address);
}

MELONDS_MCP_API unsigned int melonds_memory_read32(int cpu, unsigned int address)
{
    if (!g_nds) return 0;
    return cpu == 1 ? g_nds->ARM7Read32(address) : g_nds->ARM9Read32(address);
}

MELONDS_MCP_API int melonds_memory_read_block(int cpu, unsigned int address, int size, unsigned char* buffer)
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

MELONDS_MCP_API void melonds_memory_write8(int cpu, unsigned int address, unsigned char value)
{
    if (!g_nds) return;
    if (cpu == 1) g_nds->ARM7Write8(address, value);
    else g_nds->ARM9Write8(address, value);
}

MELONDS_MCP_API void melonds_memory_write16(int cpu, unsigned int address, unsigned short value)
{
    if (!g_nds) return;
    if (cpu == 1) g_nds->ARM7Write16(address, value);
    else g_nds->ARM9Write16(address, value);
}

MELONDS_MCP_API void melonds_memory_write32(int cpu, unsigned int address, unsigned int value)
{
    if (!g_nds) return;
    if (cpu == 1) g_nds->ARM7Write32(address, value);
    else g_nds->ARM9Write32(address, value);
}

MELONDS_MCP_API int melonds_memory_write_block(int cpu, unsigned int address, int size, const unsigned char* buffer)
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
MELONDS_MCP_API void melonds_debug_get_registers(int cpu, unsigned int* out)
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
MELONDS_MCP_API int melonds_debug_write_register(int cpu, int index, unsigned int value)
{
    ARM* arm = get_cpu(cpu);
    if (!arm) return 0;
    if (index < 0 || index > 16) return 0;

    // CPSR is not a plain scalar: mode changes swap register banks and ARM9
    // protection maps, while T changes require pipeline refilling. Until the
    // full mode-transition regression suite exists, fail explicitly.
    if (index == 16) return 0;
    if (index == 15) {
        // PC writes preserve the current instruction set. In Thumb mode a
        // conventional bit-zero marker is accepted; ARM targets must align.
        const bool thumb = (arm->CPSR & 0x20) != 0;
        if (!thumb && (value & 3)) return 0;
        const auto previousCycles = arm->Cycles;
        arm->JumpTo(thumb ? (value | 1u) : value);
        arm->Cycles = previousCycles; // Debugger writes do not execute cycles.
    } else {
        arm->R[index] = value;
    }
    return 1;
}

MELONDS_MCP_API unsigned int melonds_get_pc(int cpu)
{
    const ARM* arm = get_cpu(cpu);
    if (!arm) return 0;
    // At the stopped instruction boundary R15 includes one prefetched
    // instruction. Match the PC used by breakpoints and trace records.
    return arm->R[15] - ((arm->CPSR & 0x20) ? 2u : 4u);
}

// ═══════════════════════════════════════════
// 调试：断点
// ═══════════════════════════════════════════

MELONDS_MCP_API int melonds_debug_bp_add(int cpu, unsigned int address)
{
    return MCPDebug::AddBreakpoint(cpu == 1 ? 1 : 0, address);
}

MELONDS_MCP_API int melonds_debug_bp_remove(int bp_id)
{
    return MCPDebug::RemoveBreakpoint(bp_id) ? 1 : 0;
}

MELONDS_MCP_API int melonds_debug_bp_set_enabled(int bp_id, int enabled)
{
    return MCPDebug::SetBreakpointEnabled(bp_id, enabled != 0) ? 1 : 0;
}

// cpu: 0=ARM9 1=ARM7 -1=全部
MELONDS_MCP_API void melonds_debug_bp_clear(int cpu)
{
    MCPDebug::ClearBreakpoints(cpu < 0 ? 0xFFFFFFFF : (u32)cpu);
}

// 拷贝断点表到平行数组，返回条数
MELONDS_MCP_API int melonds_debug_bp_list(int cpu, int* out_ids, unsigned int* out_addrs,
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
MELONDS_MCP_API int melonds_debug_wp_add(int cpu, unsigned int address, unsigned int size, int kind)
{
    return MCPDebug::AddWatchpoint(cpu == 1 ? 1 : 0, address, size, (u8)(kind & 3));
}

MELONDS_MCP_API int melonds_debug_wp_remove(int wp_id)
{
    return MCPDebug::RemoveWatchpoint(wp_id) ? 1 : 0;
}

MELONDS_MCP_API int melonds_debug_wp_set_enabled(int wp_id, int enabled)
{
    return MCPDebug::SetWatchpointEnabled(wp_id, enabled != 0) ? 1 : 0;
}

MELONDS_MCP_API void melonds_debug_wp_clear(int cpu)
{
    MCPDebug::ClearWatchpoints(cpu < 0 ? 0xFFFFFFFF : (u32)cpu);
}

MELONDS_MCP_API int melonds_debug_wp_list(int cpu, int* out_ids, unsigned int* out_starts,
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

MELONDS_MCP_API void melonds_debug_step_request(int cpu, unsigned int count)
{
    MCPDebug::RequestStep(cpu == 1 ? 1 : 0, count);
}

MELONDS_MCP_API int melonds_debug_step_pending(void)
{
    return MCPDebug::StepPending() ? 1 : 0;
}

// ═══════════════════════════════════════════
// 调试：指令追踪
// ═══════════════════════════════════════════

// cpuMask: bit0=ARM9 bit1=ARM7
MELONDS_MCP_API void melonds_debug_trace_start(int cpu_mask, unsigned int addr_start, unsigned int addr_end)
{
    MCPDebug::TraceStart((u32)cpu_mask & 3, addr_start, addr_end);
}

MELONDS_MCP_API void melonds_debug_trace_stop(void)
{
    MCPDebug::TraceStop();
}

MELONDS_MCP_API int melonds_debug_trace_active(void)
{
    return MCPDebug::TraceActive() ? 1 : 0;
}

MELONDS_MCP_API unsigned int melonds_debug_trace_count(void)
{
    return MCPDebug::TraceCount();
}

// TraceEntryC 布局：{u32 cpu; u32 pc; u32 instr; u32 cpsr;}
MELONDS_MCP_API unsigned int melonds_debug_trace_drain(unsigned int* out_data, unsigned int max_entries)
{
    if (!out_data) return 0;
    return MCPDebug::DrainTrace(reinterpret_cast<MCPDebug::TraceEntry*>(out_data), max_entries);
}

// ═══════════════════════════════════════════
// 调试：命中状态与观察点事件
// ═══════════════════════════════════════════

// out[0]=hit out[1]=cpu out[2]=reason(1=bp 2=wp 3=step) out[3]=id
// out[4]=pc out[5]=addr
MELONDS_MCP_API void melonds_debug_break_info(unsigned int* out)
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

MELONDS_MCP_API void melonds_debug_break_ack(void)
{
    MCPDebug::AckBreak();
}

// WatchEventC 布局：{u32 cpu; u32 addr; u32 pc; u32 kind; u32 size; u32 value;}
// out_data 为 6*u32 平铺数组
MELONDS_MCP_API unsigned int melonds_debug_wp_events(unsigned int* out_data, unsigned int max_events)
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
MELONDS_MCP_API int melonds_debug_hooks_active(void)
{
    return MCPDebug::AnyHooksActive() ? 1 : 0;
}

MELONDS_MCP_API int melonds_debug_data_hooks_active(void)
{
    return MCPDebug::DataHooksActive() ? 1 : 0;
}

// ═══════════════════════════════════════════
// 状态查询
// ═══════════════════════════════════════════

// out[0]=running out[1]=frames out[2]=lag_frames
// out[3]=jit_enabled out[4]=console_type out[5]=rom_inserted
// out[6]=pc9 out[7]=pc7 out[8]=num_cpus(2)
MELONDS_MCP_API void melonds_get_status(unsigned int* out)
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
    out[6] = melonds_get_pc(0);
    out[7] = melonds_get_pc(1);
    out[8] = 2;
}

// 系统时钟周期（模拟运行时长度量）
MELONDS_MCP_API unsigned long long melonds_get_cycles(int num)
{
    if (!g_nds) return 0;
    return g_nds->GetSysClockCycles(num);
}

// ROM 信息。title/code/maker 为输出缓冲（各至少 16 字节）
MELONDS_MCP_API int melonds_get_rom_info(char* title, char* code, char* maker,
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

MELONDS_MCP_API void melonds_audio_enable(void)
{
    if (!g_nds) return;
    g_nds->SPU.InitOutput();
}

MELONDS_MCP_API void melonds_audio_disable(void)
{
    if (!g_nds) return;
    g_nds->SPU.DrainOutput();
}

MELONDS_MCP_API unsigned int melonds_audio_samples_available(void)
{
    if (!g_nds) return 0;
    return (unsigned int)g_nds->SPU.GetOutputSize();
}

MELONDS_MCP_API unsigned int melonds_audio_read(signed short* output, unsigned int max_frames)
{
    if (!g_nds || !output) return 0;
    int read = g_nds->SPU.ReadOutput(output, (int)max_frames);
    return (unsigned int)(read > 0 ? read : 0);
}

// ═══════════════════════════════════════════
// 存档（电池备份）
// ═══════════════════════════════════════════

MELONDS_MCP_API int melonds_backup_import(const char* filename)
{
    if (!g_nds || !filename) return 0;

    auto sav = read_file(filename);
    if (sav.empty()) return 0;

    g_nds->SetNDSSave(sav.data(), (u32)sav.size());
    return 1;
}

MELONDS_MCP_API int melonds_backup_export(const char* filename)
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

MELONDS_MCP_API void melonds_set_skip_render(int skip)
{
    if (!g_nds) return;
    g_nds->GPU.SkipRender = (skip != 0);
}

MELONDS_MCP_API int melonds_get_skip_render(void)
{
    if (!g_nds) return 0;
    return g_nds->GPU.SkipRender ? 1 : 0;
}

MELONDS_MCP_API int melonds_jit_enabled(void)
{
    if (!g_nds) return 0;
    return g_nds->IsJITEnabled() ? 1 : 0;
}

MELONDS_MCP_API int melonds_set_jit(int enabled)
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
