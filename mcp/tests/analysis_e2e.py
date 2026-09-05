"""Real MCP + native melonDS + Ghidra ARM/Thumb decompilation regression.

Requires GHIDRA_HOME and JAVA_HOME, or the corresponding command-line flags.
No commercial code is used: tiny return/add functions are written in this file.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from synthetic_rom import write_rom


async def workflow(library: Path, artifacts: Path, ghidra_home: Path | None,
                   java_home: Path | None) -> dict:
    root = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    environment.update(PYTHONPATH=str(root / "mcp/python"), MELONDS_MCP_LIB=str(library.resolve()))
    environment.pop("MELONDS_MCP_ROM", None)
    environment["MELONDS_MCP_ANALYSIS_CACHE"] = "1"
    if ghidra_home:
        environment["GHIDRA_HOME"] = str(ghidra_home.resolve())
    if java_home:
        environment["JAVA_HOME"] = str(java_home.resolve())
    params = StdioServerParameters(command=sys.executable,
                                  args=["-m", "melonds_mcp"],
                                  env=environment, cwd=str(root))
    artifacts.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="melonds-analysis-e2e-") as temp:
        rom = write_rom(Path(temp) / "analysis.nds")
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                listed = await session.list_tools()
                assert {"analysis_status", "decompile_bytes", "decompile_memory"} <= {
                    tool.name for tool in listed.tools}
                calls = []

                async def call(name, **arguments):
                    result = await session.call_tool(name, arguments)
                    assert not result.isError, (name, result.content)
                    data = result.structuredContent
                    if data is None:
                        data = json.loads(next(c.text for c in result.content if c.type == "text"))
                    assert data.get("ok", True), (name, data)
                    calls.append(name)
                    return data

                def check(result, code, base, cpu, thumb, filename):
                    assert result["kind"] == "decompilation"
                    assert result["analyzer"]["name"] == "Ghidra"
                    assert result["analyzer"]["version"]
                    assert result["analyzer"]["language_id"] == ("ARM:LE:32:v4t" if cpu else "ARM:LE:32:v5t")
                    assert result["input"]["sha256"] == hashlib.sha256(code).hexdigest()
                    assert result["input"]["size"] == len(code)
                    assert int(result["input"]["base_address"], 0) == base
                    assert int(result["input"]["entry_address"], 0) == base
                    assert int(result["function"]["entry_address"], 0) == base
                    assert result["input"]["mode"] == ("thumb" if thumb else "arm")
                    assert result["c_code_truncated"] is False
                    assert result["function"]["instruction_count"] >= 2
                    assert len(result["cfg"]["nodes"]) >= 1
                    assert "return" in result["c_code"]
                    (artifacts / filename).write_text(result["c_code"], encoding="utf-8")
                    return result

                status = await call("analysis_status")
                assert status["available"], status
                await call("load_rom", path=str(rom))
                await call("advance_frames", frames=2)
                await call("pause_emulation")
                before = await call("gpu_state")
                regs_before = [await call("read_registers", cpu=cpu) for cpu in (0, 1)]

                base = 0x02008000
                arm = bytes.fromhex("0700a0e31eff2fe1")  # mov r0,#7; bx lr
                first = check(await call("decompile_bytes", hex_data=arm.hex(),
                                         base_address=base, entry_address=base,
                                         cpu=0, thumb=False, timeout_seconds=90),
                              arm, base, 0, False, "arm9-return7.c")
                assert re.search(r"return\s+(?:0x0*7|7)\s*;", first["c_code"]), first["c_code"]
                assert first["cache"]["hit"] is False
                repeated = check(await call("decompile_bytes", hex_data=arm.hex(),
                                            base_address=base, entry_address=base,
                                            cpu=0, thumb=False, timeout_seconds=90),
                                 arm, base, 0, False, "arm9-return7-cached.c")
                assert repeated["cache"]["hit"] is True
                assert repeated["cache"]["key"] == first["cache"]["key"]
                assert repeated["c_code"] == first["c_code"]
                assert repeated["elapsed_seconds"] < 2
                assert repeated["elapsed_seconds"] < first["elapsed_seconds"] / 4
                assert "snapshot" not in repeated

                thumb = bytes.fromhex("40187047")  # adds r0,r0,r1; bx lr
                second = check(await call("decompile_bytes", hex_data=thumb.hex(),
                                          base_address=base, entry_address=base,
                                          cpu=1, thumb=True, timeout_seconds=90),
                               thumb, base, 1, True, "arm7-thumb-add.c")
                assert "+" in second["c_code"], second["c_code"]

                await call("write_memory_bytes", address=base, hex_data=arm.hex(), cpu=0)
                peeked = await call("memory_peek", address=base, length=len(arm), cpu=0)
                assert peeked["hex"] == arm.hex()
                assert peeked["access"] == "debug_peek_data_view"
                third = check(await call("decompile_memory", address=base, length=len(arm),
                                         cpu=0, thumb=False, timeout_seconds=90),
                              arm, base, 0, False, "memory-return7.c")
                changed = bytes.fromhex("0900a0e31eff2fe1")
                await call("write_memory_bytes", address=base, hex_data=changed.hex(), cpu=0)
                fourth = check(await call("decompile_memory", address=base, length=len(changed),
                                          cpu=0, thumb=False, timeout_seconds=90),
                               changed, base, 0, False, "memory-return9.c")
                assert re.search(r"return\s+(?:0x0*9|9)\s*;", fourth["c_code"]), fourth["c_code"]
                assert third["input"]["sha256"] != fourth["input"]["sha256"]
                assert third["cache"]["hit"] is True and fourth["cache"]["hit"] is False
                assert third["snapshot"]["access"] == fourth["snapshot"]["access"] == "debug_peek_instruction_backing"
                assert third["snapshot"]["view"] == fourth["snapshot"]["view"] == "instruction"
                assert third["snapshot"]["frame_number"] == before["frame_number"]
                assert fourth["snapshot"]["sha256"] == fourth["input"]["sha256"]
                data_view = check(await call("decompile_memory", address=base, length=len(changed),
                                             cpu=0, thumb=False, view="data", timeout_seconds=90),
                                  changed, base, 0, False, "memory-return9-data.c")
                assert data_view["cache"]["hit"] is True
                assert data_view["snapshot"]["access"] == "debug_peek_data_view"
                assert data_view["snapshot"]["view"] == "data"
                assert data_view["c_code"] == fourth["c_code"]
                after = await call("gpu_state")
                regs_after = [await call("read_registers", cpu=cpu) for cpu in (0, 1)]
                assert before == after and regs_before == regs_after
                invalid = await session.call_tool("decompile_bytes", {
                    "hex_data": arm.hex(), "base_address": base, "entry_address": base + 2,
                    "cpu": 0, "thumb": False})
                assert invalid.isError, "unaligned ARM entry must fail"
                invalid_peek = await session.call_tool("memory_peek", {"address": 0x04000188, "length": 4})
                assert invalid_peek.isError, "peek must never pop the emulated IPC FIFO"
                report = {"backend": "real_ghidra_via_mcp_stdio", "calls": len(calls),
                          "verified_tools": sorted(set(calls)), "state_unchanged": True,
                          "cpu_registers_unchanged": True,
                          "seconds": round(time.monotonic() - started, 3),
                          "cache_hit_seconds": repeated["elapsed_seconds"],
                          "cache_miss_seconds": first["elapsed_seconds"],
                          "analyses": [first, repeated, second, third, fourth, data_view],
                          "fixture": "original ARM return7/return9 and Thumb add functions"}
                (artifacts / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
                return {key: value for key, value in report.items() if key != "analyses"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--ghidra-home", type=Path)
    parser.add_argument("--java-home", type=Path)
    parser.add_argument("--artifacts", type=Path, default=Path("build/analysis-e2e"))
    args = parser.parse_args()
    result = asyncio.run(asyncio.wait_for(workflow(args.library, args.artifacts,
                                                  args.ghidra_home, args.java_home), timeout=420))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
