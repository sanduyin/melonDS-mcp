"""调试工具集：内存读写、寄存器监控、反汇编、断点/观察点、指令追踪、单步。"""

from __future__ import annotations

import struct
import hashlib

from .constants import (
    CPU_ARM9, CPU_ARM7, BREAK_REASON, WATCH_READ, WATCH_WRITE, WATCH_RW,
)
from .emulator import EmulatorState

try:
    import capstone  # type: ignore
    HAS_CAPSTONE = True
except ImportError:
    HAS_CAPSTONE = False


def _decode_cpsr(cpsr: int) -> dict:
    modes = {
        0x10: "usr", 0x11: "fiq", 0x12: "irq", 0x13: "svc",
        0x17: "abt", 0x1b: "und", 0x1f: "sys",
    }
    return {
        "raw": cpsr,
        "N": bool(cpsr & 0x80000000),
        "Z": bool(cpsr & 0x40000000),
        "C": bool(cpsr & 0x20000000),
        "V": bool(cpsr & 0x10000000),
        "I": bool(cpsr & 0x80),
        "F": bool(cpsr & 0x40),
        "T": bool(cpsr & 0x20),  # Thumb 状态
        "mode": modes.get(cpsr & 0x1F, f"unknown(0x{cpsr & 0x1F:02x})"),
    }


def register(mcp, emu: EmulatorState) -> None:
    lib = emu.lib.lib

    # ═══════════════ 内存读写 ═══════════════

    @mcp.tool()
    def read_memory(address: int, length: int = 4, cpu: int = 0,
                    fmt: str = "hex") -> dict:
        """读取模拟器内存。

        Args:
            address: 起始地址（如 0x02000000 为主 RAM）
            length: 读取字节数（最大 4096）
            cpu: 0=ARM9 1=ARM7（决定内存视图）
            fmt: 输出格式 hex | u8 | u16 | u32 | s8 | s16 | s32 | float | ascii
        """
        if cpu not in (CPU_ARM9, CPU_ARM7):
            return {"ok": False, "error": "cpu 必须为 0 (ARM9) 或 1 (ARM7)"}
        if not (1 <= length <= 4096):
            return {"ok": False, "error": "length 取值 1-4096"}

        data = emu.lib.read_block(cpu, address, length)

        result = {
            "ok": True,
            "address": address,
            "cpu": cpu,
            "length": length,
            "hex": data.hex(),
        }

        if fmt == "ascii":
            result["value"] = data.split(b"\x00")[0].decode("ascii", errors="replace")
        elif fmt in ("u8", "u16", "u32"):
            size = {"u8": 1, "u16": 2, "u32": 4}[fmt]
            values = []
            for off in range(0, length - size + 1, size):
                values.append(int.from_bytes(data[off:off + size], "little"))
            result["values"] = values
        elif fmt in ("s8", "s16", "s32"):
            size = {"s8": 1, "s16": 2, "s32": 4}[fmt]
            values = []
            for off in range(0, length - size + 1, size):
                values.append(int.from_bytes(data[off:off + size], "little", signed=True))
            result["values"] = values
        elif fmt == "float":
            values = []
            for off in range(0, length - 3, 4):
                values.append(struct.unpack("<f", data[off:off + 4])[0])
            result["values"] = values

        return result

    @mcp.tool()
    def write_memory(address: int, value: int, size: int = 4, cpu: int = 0) -> dict:
        """写入模拟器内存（立即生效）。

        Args:
            address: 目标地址
            value: 写入值
            size: 宽度（1/2/4 字节）
            cpu: 0=ARM9 1=ARM7
        """
        if cpu not in (CPU_ARM9, CPU_ARM7):
            return {"ok": False, "error": "cpu 必须为 0 或 1"}
        if size == 1:
            lib.melonds_memory_write8(cpu, address, value & 0xFF)
        elif size == 2:
            lib.melonds_memory_write16(cpu, address, value & 0xFFFF)
        elif size == 4:
            lib.melonds_memory_write32(cpu, address, value & 0xFFFFFFFF)
        else:
            return {"ok": False, "error": "size 必须为 1/2/4"}
        return {"ok": True, "address": address, "value": value, "size": size, "cpu": cpu}

    @mcp.tool()
    def write_memory_bytes(address: int, hex_data: str, cpu: int = 0) -> dict:
        """按十六进制字节串写入内存（如 "deadbeef" 写入 4 字节）。

        Args:
            address: 目标地址
            hex_data: 十六进制字符串（偶数长度）
            cpu: 0=ARM9 1=ARM7
        """
        try:
            data = bytes.fromhex(hex_data)
        except ValueError:
            return {"ok": False, "error": "无效的十六进制串"}
        if not data:
            return {"ok": False, "error": "空数据"}
        n = emu.lib.write_block(cpu, address, data)
        return {"ok": n == len(data), "bytes_written": n}

    # ═══════════════ 寄存器监控 ═══════════════

    @mcp.tool()
    def read_registers(cpu: int = 0) -> dict:
        """读取 CPU 完整寄存器状态（通用/状态/分组寄存器）。

        Args:
            cpu: 0=ARM9 1=ARM7
        """
        if cpu not in (CPU_ARM9, CPU_ARM7):
            return {"ok": False, "error": "cpu 必须为 0 或 1"}

        regs = emu.lib.get_registers(cpu)
        return {
            "ok": True,
            "cpu": cpu,
            "r": {f"r{i}": regs[i] for i in range(13)},
            "sp": regs[13],
            "lr": regs[14],
            "pc": regs[15],
            "instruction_address": (regs[15] - (2 if regs[16] & 0x20 else 4)) & 0xFFFFFFFF,
            "pc_semantics": "pc is raw pipeline R15; instruction_address is the next instruction",
            "cpsr": _decode_cpsr(regs[16]),
            "cycles": regs[17],
            "halted": regs[18],
            "irq_pending": regs[19],
            "banked": {
                "fiq": {f"r{8+i}": regs[22+i] for i in range(7)},
                "fiq_spsr": regs[29],
                "svc": {"sp": regs[30], "lr": regs[31], "spsr": regs[32]},
                "abt": {"sp": regs[33], "lr": regs[34], "spsr": regs[35]},
                "irq": {"sp": regs[36], "lr": regs[37], "spsr": regs[38]},
                "und": {"sp": regs[39], "lr": regs[40], "spsr": regs[41]},
            },
        }

    @mcp.tool()
    def write_register(cpu: int, name: str, value: int) -> dict:
        """写入 CPU 寄存器。

        Args:
            cpu: 0=ARM9 1=ARM7
            name: 寄存器名 r0-r12, sp, lr, pc, cpsr
            value: 写入值
        """
        if cpu not in (CPU_ARM9, CPU_ARM7):
            return {"ok": False, "error": "cpu 必须为 0 或 1"}

        name = name.lower()
        if name == "sp":
            index = 13
        elif name == "lr":
            index = 14
        elif name == "pc":
            index = 15
        elif name == "cpsr":
            index = 16
        elif name.startswith("r") and name[1:].isdigit() and 0 <= int(name[1:]) <= 12:
            index = int(name[1:])
        else:
            return {"ok": False, "error": "寄存器名须为 r0-r12/sp/lr/pc/cpsr"}

        ok = lib.melonds_debug_write_register(cpu, index, value & 0xFFFFFFFF)
        return {"ok": bool(ok), "cpu": cpu, "register": name, "value": value}

    @mcp.tool()
    def get_pc(cpu: int = 0) -> dict:
        """获取指定 CPU 下一待执行指令地址，与断点/trace 地址一致（read_registers.pc 是原始流水线 R15）。"""
        return {"cpu": cpu, "pc": lib.melonds_get_pc(cpu)}

    # ═══════════════ 反汇编 ═══════════════

    @mcp.tool()
    def disassemble(address: int, count: int = 10, cpu: int = 0,
                    thumb: bool | None = None) -> dict:
        """反汇编安全指令 backing（含 ARM9 ITCM，排除 DTCM；需安装 capstone）。

        Args:
            address: 起始地址
            count: 指令条数（默认 10）
            cpu: 0=ARM9 1=ARM7
            thumb: 强制 Thumb 模式；缺省按当前 CPSR 的 T 位判断
        """
        if not HAS_CAPSTONE:
            return {"ok": False,
                    "error": "未安装 capstone（pip install capstone）"}

        if thumb is None:
            regs = emu.lib.get_registers(cpu)
            thumb = bool(regs[16] & 0x20)

        if address % (2 if thumb else 4):
            raise ValueError("反汇编地址必须按 ARM 4 字节 / Thumb 2 字节对齐")
        # ARM and long Thumb instructions are at most four bytes. Never use
        # bus reads here: they omit ITCM and may consume MMIO/device state.
        data = emu.lib.code_peek_block(cpu, address, count * 4)

        md = capstone.Cs(capstone.CS_ARCH_ARM,
                         capstone.CS_MODE_THUMB if thumb else capstone.CS_MODE_ARM)
        instructions = []
        for ins in md.disasm(data, address):
            instructions.append({
                "address": ins.address,
                "bytes": ins.bytes.hex(),
                "mnemonic": ins.mnemonic,
                "op_str": ins.op_str,
            })
            if len(instructions) >= count:
                break

        return {"ok": True, "address": address, "cpu": cpu, "thumb": thumb,
                "instructions": instructions, "byte_length": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "access": "debug_peek_instruction_backing"}

    # ═══════════════ 断点 ═══════════════

    @mcp.tool()
    def breakpoint_add(cpu: int, address: int) -> dict:
        """添加指令断点（PC 到达指定地址时暂停；调试期间自动切换解释器）。

        Args:
            cpu: 0=ARM9 1=ARM7
            address: 断点地址（真实 PC）
        """
        bp_id = lib.melonds_debug_bp_add(cpu, address)
        if bp_id < 0:
            return {"ok": False, "error": "添加失败（重复或超出上限 64）"}
        return {"ok": True, "id": bp_id, "cpu": cpu, "address": address}

    @mcp.tool()
    def breakpoint_remove(bp_id: int) -> dict:
        """按 ID 删除断点。"""
        ok = lib.melonds_debug_bp_remove(bp_id)
        return {"ok": bool(ok), "id": bp_id}

    @mcp.tool()
    def breakpoint_list(cpu: int = -1) -> dict:
        """列出断点（可按 CPU 过滤）。

        Args:
            cpu: 0=ARM9 1=ARM7 -1=全部
        """
        return {"breakpoints": emu.lib.bp_list(cpu)}

    @mcp.tool()
    def breakpoint_clear(cpu: int = -1) -> dict:
        """清除断点（可按 CPU 过滤）。"""
        lib.melonds_debug_bp_clear(cpu)
        return {"ok": True}

    # ═══════════════ 观察点 ═══════════════

    @mcp.tool()
    def watchpoint_add(cpu: int, address: int, size: int = 4,
                       kind: str = "rw") -> dict:
        """添加数据观察点（CPU 访问指定内存范围时暂停并记录事件）。

        Args:
            cpu: 0=ARM9 1=ARM7
            address: 监视起始地址
            size: 监视字节数
            kind: r=读 w=写 rw=读写
        """
        kind_map = {"r": WATCH_READ, "w": WATCH_WRITE, "rw": WATCH_RW}
        if kind not in kind_map:
            return {"ok": False, "error": "kind 须为 r/w/rw"}
        wp_id = lib.melonds_debug_wp_add(cpu, address, size, kind_map[kind])
        if wp_id < 0:
            return {"ok": False, "error": "添加失败（重叠或超出上限 64）"}
        return {"ok": True, "id": wp_id, "cpu": cpu, "address": address,
                "size": size, "kind": kind}

    @mcp.tool()
    def watchpoint_remove(wp_id: int) -> dict:
        """按 ID 删除观察点。"""
        ok = lib.melonds_debug_wp_remove(wp_id)
        return {"ok": bool(ok), "id": wp_id}

    @mcp.tool()
    def watchpoint_list(cpu: int = -1) -> dict:
        """列出观察点。"""
        return {"watchpoints": emu.lib.wp_list(cpu)}

    @mcp.tool()
    def watchpoint_clear(cpu: int = -1) -> dict:
        """清除观察点。"""
        lib.melonds_debug_wp_clear(cpu)
        return {"ok": True}

    @mcp.tool()
    def watchpoint_events() -> dict:
        """取出并清空观察点事件缓冲（每次命中记录 CPU/地址/PC/读写方向/值）。"""
        events = emu.lib.wp_events()
        for e in events:
            e["kind"] = {1: "read", 2: "write"}.get(e["kind"], str(e["kind"]))
        return {"events": events}

    # ═══════════════ 指令追踪 ═══════════════

    @mcp.tool()
    def trace_start(cpu_mask: int = 1, address_start: int = 0,
                    address_end: int = 0xFFFFFFFF) -> dict:
        """开始指令追踪（环形缓冲 65536 条，超出后覆盖最旧记录）。

        Args:
            cpu_mask: 1=仅ARM9 2=仅ARM7 3=双CPU
            address_start: 追踪地址范围起点（PC 过滤）
            address_end: 追踪地址范围终点
        """
        lib.melonds_debug_trace_start(cpu_mask & 3, address_start, address_end)
        return {"ok": True, "cpu_mask": cpu_mask & 3,
                "address_start": address_start, "address_end": address_end}

    @mcp.tool()
    def trace_stop() -> dict:
        """停止指令追踪。"""
        lib.melonds_debug_trace_stop()
        return {"ok": True}

    @mcp.tool()
    def trace_get(max_entries: int = 1000) -> dict:
        """取出并清空追踪缓冲（每条含 CPU/PC/指令码/CPSR）。

        Args:
            max_entries: 最多取回条数（默认 1000，最大 65536）
        """
        if not lib.melonds_debug_trace_active():
            count = lib.melonds_debug_trace_count()
            if count == 0:
                return {"entries": [], "note": "追踪未激活且缓冲为空"}
        entries = emu.lib.trace_drain(min(max_entries, 65536))
        return {"count": len(entries), "entries": entries}

    # ═══════════════ 单步与断点控制 ═══════════════

    @mcp.tool()
    def step(count: int = 1, cpu: int = 0) -> dict:
        """单步执行指定条数的指令后暂停。

        Args:
            count: 指令条数（默认 1）
            cpu: 0=ARM9 1=ARM7
        """
        return emu.step(cpu, count)

    @mcp.tool()
    def get_break_info() -> dict:
        """查询当前断点命中状态（原因/位置/命中的 ID）。"""
        info = emu.lib.break_info()
        info["reason"] = BREAK_REASON.get(info["reason"], str(info["reason"]))
        return info

    @mcp.tool()
    def continue_after_break() -> dict:
        """确认断点命中并继续执行（配合 run_until_break 使用）。"""
        emu.continue_after_break()
        return {"ok": True, "resumed": True}
