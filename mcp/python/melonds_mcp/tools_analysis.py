"""Optional genuine decompilation tools, separate from Capstone disassembly."""

from typing import Literal

from . import decompiler


def register(mcp, emu) -> None:
    @mcp.tool()
    def analysis_status() -> dict:
        """Check optional Ghidra/JDK availability; no analysis process is launched."""
        return decompiler.analysis_status()

    @mcp.tool()
    def decompile_bytes(hex_data: str, base_address: int, entry_address: int,
                        thumb: bool = False, timeout_seconds: int = 60, cpu: int = 0) -> dict:
        """Decompile supplied little-endian ARM/Thumb bytes into real Ghidra C pseudocode and CFG.

        Requires GHIDRA_HOME and JAVA_HOME. Entry must be aligned and inside the
        supplied bytes; use thumb=True instead of setting address bit zero.
        """
        return decompiler.decompile_bytes(hex_data, base_address, entry_address,
                                          thumb, timeout_seconds, cpu)

    @mcp.tool()
    def decompile_memory(address: int, length: int = 256, cpu: int = 0,
                         thumb: bool = False, timeout_seconds: int = 60,
                         view: Literal["instruction", "data"] = "instruction") -> dict:
        """Decompile a safe backing-memory snapshot with hash/frame provenance.

        Default instruction view includes ARM9 ITCM but ignores the DTCM data
        overlay. Explicit view='data' inspects the CPU data backing instead.
        Neither view is an I-cache/prefetch snapshot. MMIO and unsupported ranges
        are refused. Supply a known entry: arbitrary RAM can mix code and data.
        """
        if type(address) is not int or type(length) is not int or not 1 <= length <= 4096:
            raise ValueError("address must be an integer; length must be 1..4096")
        if type(cpu) is not int or cpu not in (0, 1):
            raise ValueError("cpu must be 0 or 1")
        if not 0 <= address < address + length <= 0x100000000:
            raise ValueError("input range exceeds the 32-bit address space")
        if view not in ("instruction", "data"):
            raise ValueError("view must be instruction or data")
        # ToolBoundary holds the emulator's transaction lock for this call.
        status = emu.lib.get_status()
        read = emu.lib.code_peek_block if view == "instruction" else emu.lib.peek_block
        data = read(cpu, address, length)
        if len(data) != length:
            raise RuntimeError("Incomplete emulator memory snapshot")
        result = decompiler.decompile_bytes(data.hex(), address, address,
                                           thumb, timeout_seconds, cpu)
        result["snapshot"] = {"source": "live_emulator_ram_copy", "frame_number": status[1],
                              "cpu": cpu, "address": f"0x{address:08x}", "length": length,
                              "view": view,
                              "access": "debug_peek_instruction_backing" if view == "instruction" else "debug_peek_data_view",
                              "read_semantics": "instruction backing, ARM9 excludes DTCM; not I-cache/prefetch" if view == "instruction" else "CPU data backing, including DTCM; not I-cache/prefetch",
                              "sha256": result["input"]["sha256"]}
        return result
