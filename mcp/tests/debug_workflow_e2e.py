"""Real MCP stdio regression for debugger discovery, trace, and watch workflows.

Run with mcp/.venv/Scripts/python.exe mcp/tests/debug_workflow_e2e.py
--library build/mcp-direct/melonds_mcp.dll. The ROM and instruction snippets
are original fixtures; no commercial data or external service is required.
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from synthetic_rom import CODE_BASES, write_rom


ROM_OPCODES = {
    0x00: 0xE59F000C,  # ldr r0,[pc,#12]
    0x04: 0xE3A01000,  # mov r1,#0
    0x08: 0xE2811001,  # add r1,r1,#1
    0x0C: 0xE5801000,  # str r1,[r0]
    0x10: 0xEAFFFFFC,  # b <add>
}


def arm(*words: int) -> bytes:
    return struct.pack("<" + "I" * len(words), *words)


async def workflow(library: Path, artifacts: Path) -> dict:
    root = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    environment.update(PYTHONPATH=str(root / "mcp/python"),
                       MELONDS_MCP_LIB=str(library.resolve()))
    environment.pop("MELONDS_MCP_ROM", None)
    server = StdioServerParameters(command=sys.executable, args=["-m", "melonds_mcp"],
                                   env=environment, cwd=str(root))
    artifacts.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="melonds-debug-e2e-") as temp:
        rom = write_rom(Path(temp) / "debug.nds")
        async with stdio_client(server) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                listed = await session.list_tools()
                required = {
                    "load_rom", "advance_frames", "write_memory", "memory_poke",
                    "code_patch", "code_peek", "write_register", "get_pc",
                    "disassemble", "breakpoint_add", "breakpoint_list",
                    "breakpoint_remove", "watchpoint_add", "watchpoint_list",
                    "watchpoint_events", "watchpoint_remove", "watchpoint_clear",
                    "trace_start", "trace_get", "trace_stop", "step",
                    "get_break_info", "continue_after_break",
                }
                names = {tool.name for tool in listed.tools}
                assert required <= names, sorted(required - names)
                completed: list[str] = []

                async def call(tool_name: str, **arguments):
                    result = await session.call_tool(tool_name, arguments)
                    assert not result.isError, (tool_name, result.content)
                    data = result.structuredContent
                    if data is None:
                        texts = [item.text for item in result.content if item.type == "text"]
                        data = json.loads(texts[0]) if texts else {}
                    assert data.get("ok", True), (tool_name, data)
                    completed.append(tool_name)
                    return data

                await call("load_rom", path=str(rom))

                # The two direct-boot PCs and the original ROM code must be
                # observable without first advancing a frame.
                initial_pcs = []
                for cpu, base in enumerate(CODE_BASES):
                    pc = await call("get_pc", cpu=cpu)
                    assert pc == {"cpu": cpu, "pc": base}, pc
                    initial_pcs.append(pc["pc"])
                    decoded = await call("disassemble", cpu=cpu, address=base,
                                         count=5, thumb=False)
                    assert decoded["access"] == "debug_peek_instruction_backing"
                    assert decoded["byte_length"] == 20
                    assert [item["address"] for item in decoded["instructions"]] == [
                        base + offset for offset in ROM_OPCODES]
                    assert [int.from_bytes(bytes.fromhex(item["bytes"]), "little")
                            for item in decoded["instructions"]] == list(ROM_OPCODES.values())

                # Trace each CPU separately so a bounded drain proves exact
                # CPU, PC and original opcode rather than merely non-empty data.
                trace_evidence = []
                for cpu, base in enumerate(CODE_BASES):
                    await call("trace_start", cpu_mask=1 << cpu,
                               address_start=base, address_end=base + 0x10)
                    progressed = await call("step", cpu=cpu, count=8)
                    assert progressed["hit"], progressed
                    await call("trace_stop")
                    drained = await call("trace_get", max_entries=1000)
                    entries = drained["entries"]
                    # Trace records arrived instruction boundaries, including
                    # the ninth boundary where the eighth step stops execution.
                    assert drained["count"] == len(entries) == 9, drained
                    assert {entry["cpu"] for entry in entries} == {cpu}
                    for entry in entries:
                        offset = entry["pc"] - base
                        assert offset in ROM_OPCODES, entry
                        assert entry["instr"] == ROM_OPCODES[offset], entry
                        assert not (entry["cpsr"] & 0x20), entry
                    trace_evidence.append({"cpu": cpu, "entries": len(entries),
                                           "first": entries[0], "last": entries[-1]})
                    empty = await call("trace_get", max_entries=1)
                    assert empty["entries"] == [], empty
                    await call("continue_after_break")

                # Breakpoint management is tested independently of execution:
                # global and per-CPU lists must preserve the actual IDs.
                bp9 = await call("breakpoint_add", cpu=0, address=CODE_BASES[0] + 8)
                bp7 = await call("breakpoint_add", cpu=1, address=CODE_BASES[1] + 12)
                both = await call("breakpoint_list", cpu=-1)
                assert {item["id"] for item in both["breakpoints"]} == {bp9["id"], bp7["id"]}
                only7 = await call("breakpoint_list", cpu=1)
                assert only7["breakpoints"] == [{"id": bp7["id"],
                                                  "address": CODE_BASES[1] + 12,
                                                  "cpu": 1, "enabled": True}], only7
                await call("breakpoint_remove", bp_id=bp9["id"])
                remaining = await call("breakpoint_list", cpu=-1)
                assert [item["id"] for item in remaining["breakpoints"]] == [bp7["id"]]
                await call("breakpoint_remove", bp_id=bp7["id"])
                assert (await call("breakpoint_list", cpu=-1))["breakpoints"] == []

                # Safe disassembly must honor the ARM9 instruction view. ITCM
                # appears in both ARM and Thumb modes; DTCM data at 0x03000000
                # must not mask underlying shared-WRAM instructions.
                arm_itcm = arm(0xE3A02055, 0xE12FFF1E)  # mov r2,#0x55; bx lr
                await call("code_patch", cpu=0, address=0x00001000,
                           hex_data=arm_itcm.hex())
                decoded_arm = await call("disassemble", cpu=0, address=0x01001000,
                                         count=2, thumb=False)
                assert [item["mnemonic"] for item in decoded_arm["instructions"]] == ["mov", "bx"]
                assert decoded_arm["sha256"] == hashlib.sha256(arm_itcm).hexdigest()

                thumb_itcm = bytes.fromhex("55227047")  # movs r2,#0x55; bx lr
                await call("code_patch", cpu=0, address=0x00001020,
                           hex_data=thumb_itcm.hex())
                decoded_thumb = await call("disassemble", cpu=0, address=0x01001020,
                                           count=1, thumb=True)
                assert decoded_thumb["instructions"][0]["mnemonic"] == "movs"
                assert decoded_thumb["instructions"][0]["op_str"] == "r2, #0x55"

                await call("write_memory", cpu=0, address=0x04000247, size=1, value=0)
                data_overlay = bytes.fromhex("deadbeef")
                code_underlay = arm(0xE3A07066)  # mov r7,#0x66
                await call("memory_poke", cpu=0, address=0x03000080,
                           hex_data=data_overlay.hex())
                await call("code_patch", cpu=0, address=0x03000080,
                           hex_data=code_underlay.hex())
                decoded_overlay = await call("disassemble", cpu=0, address=0x03000080,
                                             count=1, thumb=False)
                assert decoded_overlay["instructions"][0]["mnemonic"] == "mov"
                assert decoded_overlay["instructions"][0]["op_str"] == "r7, #0x66"
                assert decoded_overlay["sha256"] == hashlib.sha256(code_underlay).hexdigest()

                # A two-instruction program creates one write and one read hit.
                # Repeat on both CPUs and verify the event identity and payload.
                watch_evidence = []
                program = arm(0xE5801000, 0xE5902000, 0xEAFFFFFE)  # str/ldr/b .
                for cpu in (0, 1):
                    code = 0x0200A000 + cpu * 0x100
                    target = 0x0200B000 + cpu * 0x100
                    marker = 0x10203040 + cpu
                    await call("code_patch", cpu=cpu, address=code, hex_data=program.hex())
                    await call("write_register", cpu=cpu, name="r0", value=target)
                    await call("write_register", cpu=cpu, name="r1", value=marker)
                    await call("write_register", cpu=cpu, name="pc", value=code)
                    wp = await call("watchpoint_add", cpu=cpu, address=target,
                                    size=4, kind="rw")
                    listed_wp = await call("watchpoint_list", cpu=cpu)
                    assert listed_wp["watchpoints"] == [{
                        "id": wp["id"], "start": target, "end": target + 3,
                        "cpu": cpu, "kind": 3, "enabled": True}], listed_wp

                    write_step = await call("step", cpu=cpu, count=1)
                    assert write_step["hit"]
                    hit = await call("get_break_info")
                    assert (hit["hit"], hit["cpu"], hit["reason"], hit["id"],
                            hit["pc"], hit["addr"]) == (
                                True, cpu, "watchpoint", wp["id"], code, target), hit
                    write_events = await call("watchpoint_events")
                    assert write_events["events"] == [{
                        "cpu": cpu, "address": target, "pc": code,
                        "kind": "write", "size": 4, "value": marker}], write_events

                    read_step = await call("step", cpu=cpu, count=1)
                    assert read_step["hit"]
                    read_hit = await call("get_break_info")
                    assert (read_hit["cpu"], read_hit["reason"], read_hit["id"],
                            read_hit["pc"], read_hit["addr"]) == (
                                cpu, "watchpoint", wp["id"], code + 4, target), read_hit
                    read_events = await call("watchpoint_events")
                    assert read_events["events"] == [{
                        "cpu": cpu, "address": target, "pc": code + 4,
                        "kind": "read", "size": 4, "value": 0}], read_events
                    watch_evidence.append({"cpu": cpu, "write": write_events["events"][0],
                                           "read": read_events["events"][0],
                                           "read_value_contract": "zero (actual read value is not captured)"})
                    await call("watchpoint_remove", wp_id=wp["id"])
                    assert (await call("watchpoint_list", cpu=cpu))["watchpoints"] == []
                    await call("continue_after_break")

                # Clear one CPU independently, then clear all.
                clear9 = await call("watchpoint_add", cpu=0, address=0x0200C000,
                                    size=1, kind="w")
                clear7 = await call("watchpoint_add", cpu=1, address=0x0200C100,
                                    size=1, kind="r")
                await call("watchpoint_clear", cpu=0)
                after_cpu_clear = await call("watchpoint_list", cpu=-1)
                assert [item["id"] for item in after_cpu_clear["watchpoints"]] == [clear7["id"]]
                await call("watchpoint_clear", cpu=-1)
                assert (await call("watchpoint_list", cpu=-1))["watchpoints"] == []
                assert clear9["id"] != clear7["id"]

                # Global input boundary must reject an invalid CPU before it
                # reaches the historically permissive native ABI.
                rejected = await session.call_tool("get_pc", {"cpu": 2})
                assert rejected.isError, rejected.content

                report = {
                    "backend": "real_native_debugger_via_mcp_stdio",
                    "library": str(library.resolve()),
                    "tool_count": len(listed.tools),
                    "calls": len(completed),
                    "verified_tools": sorted(set(completed)),
                    "initial_pcs": initial_pcs,
                    "trace": trace_evidence,
                    "watchpoints": watch_evidence,
                    "disassembly": {
                        "access": "debug_peek_instruction_backing",
                        "arm9_itcm_arm": decoded_arm["instructions"],
                        "arm9_itcm_thumb": decoded_thumb["instructions"],
                        "dtcm_excluded": True,
                        "underlay": decoded_overlay["instructions"],
                    },
                    "invalid_cpu_rejected_at_mcp_boundary": True,
                    "fixture": "generated dual-CPU ARM loops plus original ARM/Thumb snippets",
                }
                (artifacts / "result.json").write_text(
                    json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
                return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path,
                        default=Path("build/debug-workflow-e2e"))
    parser.add_argument("--timeout", type=float, default=90)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    result = asyncio.run(asyncio.wait_for(workflow(args.library, args.artifacts),
                                          timeout=args.timeout))
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
