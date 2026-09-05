"""Real MCP stdio regression for safe data pokes and coherent code patches.

The test uses ``synthetic_rom.py`` only; it needs no commercial ROM, firmware,
GUI session, or network access.  It deliberately checks behavior through the
published MCP tools instead of importing the Python facade directly.

Run with::

    mcp/.venv/Scripts/python.exe mcp/tests/memory_poke_e2e.py \
        --library build/mcp-direct/melonds_mcp.dll

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from synthetic_rom import write_rom


WRAM_CONTROL = 0x04000247
SNAPSHOT_ADDRESS = 0x0203F000


def arm_hex(*words: int) -> str:
    return struct.pack("<" + "I" * len(words), *words).hex()


def content_text(result: Any) -> str:
    return "\n".join(
        item.text for item in result.content
        if getattr(item, "type", None) == "text" and hasattr(item, "text")
    )


async def workflow(library: Path, artifacts: Path) -> dict:
    root = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(root / "mcp" / "python")
    environment["MELONDS_MCP_LIB"] = str(library.resolve())
    environment.pop("MELONDS_MCP_ROM", None)
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "melonds_mcp"],
        env=environment,
        cwd=str(root),
    )
    artifacts.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="melonds-mcp-poke-e2e-") as temp:
        rom = write_rom(Path(temp) / "memory-poke.nds")
        async with stdio_client(server) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                listed = await session.list_tools()
                available = {tool.name for tool in listed.tools}
                required = {
                    "load_rom", "pause_emulation", "resume_emulation",
                    "get_status", "read_memory", "write_memory",
                    "read_registers", "write_register", "step",
                    "continue_after_break", "memory_peek", "code_peek",
                    "memory_poke", "code_patch",
                }
                assert required <= available, sorted(required - available)

                calls: list[dict[str, str]] = []

                async def call(tool_name: str, **arguments: Any) -> dict:
                    result = await session.call_tool(tool_name, arguments)
                    calls.append({"tool": tool_name, "outcome": "ok"})
                    assert not result.isError, (tool_name, content_text(result), result.content)
                    data = result.structuredContent
                    if data is None:
                        text = content_text(result)
                        data = json.loads(text) if text else {}
                    assert isinstance(data, dict), (tool_name, data)
                    assert data.get("ok", True), (tool_name, data)
                    return data

                async def reject(tool_name: str, contains: str | None = None,
                                 **arguments: Any) -> str:
                    result = await session.call_tool(tool_name, arguments)
                    calls.append({"tool": tool_name, "outcome": "rejected"})
                    text = content_text(result)
                    assert result.isError, (tool_name, result.structuredContent, text)
                    if contains is not None:
                        assert contains.lower() in text.lower(), (tool_name, contains, text)
                    return text

                async def peek(name: str, cpu: int, address: int,
                               length: int) -> dict:
                    return await call(name, cpu=cpu, address=address, length=length)

                async def snapshot() -> dict:
                    """Exact non-running state relevant to safe debugger writes.

                    The frame number is taken from side-effect-free memory_peek.
                    Complete register responses include both CPSRs and CPU cycles.
                    """
                    regs = [await call("read_registers", cpu=cpu) for cpu in (0, 1)]
                    frame = (await peek("memory_peek", 0, SNAPSHOT_ADDRESS, 1))["frame_number"]
                    return {"frame": frame, "registers": regs}

                await call("load_rom", path=str(rom))
                await call("pause_emulation")

                evidence: dict[str, Any] = {}

                # A guarded data-view write succeeds only against current bytes.
                ram_address = 0x02003000
                ram_before = await peek("memory_peek", 0, ram_address, 4)
                state_before = await snapshot()
                ram_write = await call(
                    "memory_poke", cpu=0, address=ram_address,
                    hex_data="44332211", expected_hex=ram_before["hex"],
                )
                assert ram_write["hex"] == "44332211", ram_write
                assert ram_write["previous_hex"] == ram_before["hex"], ram_write
                assert ram_write["expected_bytes_checked"] is True, ram_write
                assert ram_write["access"] == "debug_poke_data_view", ram_write
                assert ram_write["prefetch_refresh_requested"] is False, ram_write
                assert await snapshot() == state_before, "memory_poke changed CPU/frame state"

                # A stale expected-byte guard must fail atomically.
                state_before = await snapshot()
                await reject(
                    "memory_poke", "EXPECTED_BYTES_MISMATCH", cpu=0,
                    address=ram_address, hex_data="88776655",
                    expected_hex=ram_before["hex"],
                )
                ram_after_guard = await peek("memory_peek", 0, ram_address, 4)
                assert ram_after_guard["hex"] == "44332211", ram_after_guard
                assert await snapshot() == state_before, "failed guard changed CPU/frame state"
                evidence["guarded_data_write"] = {
                    "address": ram_address,
                    "before": ram_before["hex"],
                    "after_success": ram_write["hex"],
                    "after_stale_guard": ram_after_guard["hex"],
                }

                # Establish an underlying shared-WRAM word beneath ARM9 DTCM.
                # Legacy write_memory is intentionally used only for fixture setup.
                dtcm_address = 0x03000020
                await call("write_memory", cpu=0, address=WRAM_CONTROL, size=1, value=0)
                await call("write_memory", cpu=0, address=dtcm_address, size=4,
                           value=0x76543210)
                dtcm_before = await peek("memory_peek", 0, dtcm_address, 4)
                code_before = await peek("code_peek", 0, dtcm_address, 4)
                assert code_before["hex"] == "10325476", code_before
                assert dtcm_before["access"] == "debug_peek_data_view", dtcm_before
                assert code_before["access"] == "debug_peek_instruction_backing", code_before

                state_before = await snapshot()
                dtcm_write = await call(
                    "memory_poke", cpu=0, address=dtcm_address,
                    hex_data="efcdab89", expected_hex=dtcm_before["hex"],
                )
                assert await snapshot() == state_before, "DTCM poke changed CPU/frame state"
                assert dtcm_write["hex"] == "efcdab89", dtcm_write
                assert (await peek("code_peek", 0, dtcm_address, 4))["hex"] == "10325476"

                state_before = await snapshot()
                dtcm_code = await call(
                    "code_patch", cpu=0, address=dtcm_address,
                    hex_data=arm_hex(0xE3A02066), expected_hex="10325476",
                )
                assert await snapshot() == state_before, "DTCM code patch changed CPU/frame state"
                assert dtcm_code["access"] == "debug_patch_instruction_backing", dtcm_code
                assert dtcm_code["prefetch_refresh_requested"] is True, dtcm_code
                assert (await peek("memory_peek", 0, dtcm_address, 4))["hex"] == "efcdab89"
                assert (await peek("code_peek", 0, dtcm_address, 4))["hex"] == arm_hex(0xE3A02066)
                evidence["dtcm_views"] = {
                    "address": dtcm_address,
                    "data_view": "efcdab89",
                    "instruction_view": arm_hex(0xE3A02066),
                }

                # Put already-prefetched ARM instructions at each CPU's current PC,
                # then patch through the other CPU and a different main-RAM mirror.
                old_program = arm_hex(0xE3A02011, 0xE3A03022, 0xEAFFFFFE)
                old_prefix = arm_hex(0xE3A02011, 0xE3A03022)
                new_prefix = arm_hex(0xE3A02066, 0xE3A03077)
                bad_prefix = arm_hex(0xE3A02055, 0xE3A03044)
                execution: list[dict[str, Any]] = []
                for cpu in (0, 1):
                    address = 0x02008000 + cpu * 0x100
                    initial = await peek("code_peek", cpu, address, 12)
                    await call(
                        "code_patch", cpu=cpu, address=address,
                        hex_data=old_program, expected_hex=initial["hex"],
                    )
                    await call("write_register", cpu=cpu, name="r2", value=0)
                    await call("write_register", cpu=cpu, name="r3", value=0)
                    await call("write_register", cpu=cpu, name="pc", value=address)
                    armed = await call("read_registers", cpu=cpu)
                    assert armed["cpsr"]["T"] is False, armed

                    state_before = await snapshot()
                    alias = address + 0x00400000
                    patch = await call(
                        "code_patch", cpu=1 - cpu, address=alias,
                        hex_data=new_prefix, expected_hex=old_prefix,
                    )
                    assert patch["previous_hex"] == old_prefix, patch
                    assert patch["hex"] == new_prefix, patch
                    assert await snapshot() == state_before, (
                        f"CPU{cpu} prefetched code patch changed registers/frame/CPSR")
                    assert (await peek("code_peek", cpu, address, 8))["hex"] == new_prefix
                    assert (await peek("code_peek", 1 - cpu, alias, 8))["hex"] == new_prefix

                    # Stale code guard must neither write nor disturb the pipeline.
                    state_before = await snapshot()
                    await reject(
                        "code_patch", "EXPECTED_BYTES_MISMATCH", cpu=cpu,
                        address=address, hex_data=bad_prefix,
                        expected_hex=old_prefix,
                    )
                    assert (await peek("code_peek", cpu, address, 8))["hex"] == new_prefix
                    assert await snapshot() == state_before, (
                        f"CPU{cpu} failed code guard changed registers/frame/CPSR")

                    await call("resume_emulation")
                    stepped = await call("step", cpu=cpu, count=2)
                    assert stepped["hit"] is True, stepped
                    assert stepped["break_info"]["cpu"] == cpu, stepped
                    assert stepped["break_info"]["reason"] == 3, stepped
                    after_step = await call("read_registers", cpu=cpu)
                    assert after_step["r"]["r2"] == 0x66, after_step
                    assert after_step["r"]["r3"] == 0x77, after_step
                    execution.append({
                        "cpu": cpu, "pc": address, "patch_alias": alias,
                        "r2": after_step["r"]["r2"], "r3": after_step["r"]["r3"],
                        "step_frames": stepped["frames_executed"],
                    })
                    await call("continue_after_break")
                    await call("pause_emulation")
                evidence["prefetch_patch_execution"] = execution

                # BIOS is readable but immutable; verify both CPU views before/after.
                bios_cases = ((0, 0xFFFF0000), (1, 0x00000000))
                rejected: list[dict[str, Any]] = []
                for cpu, address in bios_cases:
                    before_data = await peek("memory_peek", cpu, address, 4)
                    before_code = await peek("code_peek", cpu, address, 4)
                    state_before = await snapshot()
                    for tool in ("memory_poke", "code_patch"):
                        message = await reject(
                            tool, "debug write refused", cpu=cpu, address=address,
                            hex_data="a5a5a5a5",
                        )
                        rejected.append({"tool": tool, "cpu": cpu,
                                         "address": address, "kind": "BIOS",
                                         "diagnostic": message[:240]})
                    assert (await peek("memory_peek", cpu, address, 4))["hex"] == before_data["hex"]
                    assert (await peek("code_peek", cpu, address, 4))["hex"] == before_code["hex"]
                    assert await snapshot() == state_before, "rejected BIOS write changed execution state"

                # MMIO peeks are intentionally unavailable.  Use the existing,
                # explicitly side-effectful bus tool only on safe DISPCNT for the
                # before/after proof; neither debugger write may reach the bus.
                mmio_address = 0x04000000
                for cpu in (0, 1):
                    mmio_before = await call(
                        "read_memory", cpu=cpu, address=mmio_address,
                        length=4, fmt="u32",
                    )
                    state_before = await snapshot()
                    for tool in ("memory_poke", "code_patch"):
                        message = await reject(
                            tool, "debug peek refused", cpu=cpu,
                            address=mmio_address, hex_data="ffffffff",
                        )
                        rejected.append({"tool": tool, "cpu": cpu,
                                         "address": mmio_address, "kind": "MMIO",
                                         "diagnostic": message[:240]})
                    mmio_after = await call(
                        "read_memory", cpu=cpu, address=mmio_address,
                        length=4, fmt="u32",
                    )
                    assert mmio_after["hex"] == mmio_before["hex"], (mmio_before, mmio_after)
                    assert await snapshot() == state_before, "rejected MMIO write changed execution state"
                evidence["rejected_ranges"] = rejected

                result = {
                    "backend": "real_native_dll_via_mcp_stdio",
                    "library": str(library.resolve()),
                    "tool_count": len(listed.tools),
                    "calls": len(calls),
                    "call_outcomes": {
                        "ok": sum(call_["outcome"] == "ok" for call_ in calls),
                        "rejected_as_expected": sum(
                            call_["outcome"] == "rejected" for call_ in calls),
                    },
                    "verified_tools": sorted({call_["tool"] for call_ in calls}),
                    "evidence": evidence,
                    "rom": "generated ARM9/ARM7 synthetic fixture; no commercial data",
                }
                (artifacts / "result.json").write_text(
                    json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8",
                )
                return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument(
        "--artifacts", type=Path, default=Path("build/memory-poke-e2e"),
    )
    args = parser.parse_args()
    result = asyncio.run(
        asyncio.wait_for(workflow(args.library, args.artifacts), timeout=60),
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
