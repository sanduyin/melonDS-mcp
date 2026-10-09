"""Real MCP control/status/input/watch/savestate regression; no commercial ROM.

Input assertions use both hardware registers and an ARM7 program which samples
KEYINPUT/EXTKEYIN during press/tap calls. This proves the transient inputs reach
the running guest before the tools release them.
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

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from synthetic_rom import CODE_BASES, COUNTERS, ROM_SIZE, write_rom


async def workflow(library: Path, artifacts: Path) -> dict:
    root = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(root / "mcp" / "python")
    environment["MELONDS_MCP_LIB"] = str(library.resolve())
    environment.pop("MELONDS_MCP_ROM", None)
    server = StdioServerParameters(command=sys.executable,
        args=["-m", "melonds_mcp"], env=environment, cwd=str(root))
    artifacts.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="melonds-control-e2e-") as temp:
        temporary = Path(temp)
        rom = write_rom(temporary / "synthetic.nds")
        async with stdio_client(server) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                completed: list[str] = []
                rejected: list[str] = []
                evidence: dict = {}

                async def invoke(tool_name: str, **arguments):
                    response = await session.call_tool(tool_name, arguments)
                    data = response.structuredContent
                    if data is None:
                        texts = [item.text for item in response.content if item.type == "text"]
                        try:
                            data = json.loads(texts[0]) if texts else {}
                        except json.JSONDecodeError:
                            data = {"error": "\n".join(texts)}
                    return response, data

                async def call(tool_name: str, **arguments):
                    response, data = await invoke(tool_name, **arguments)
                    assert not response.isError and data.get("ok", True), (tool_name, data)
                    completed.append(tool_name)
                    return data

                async def reject(tool_name: str, **arguments):
                    response, data = await invoke(tool_name, **arguments)
                    assert response.isError or data.get("ok") is False, (tool_name, data)
                    rejected.append(tool_name)
                    return data

                async def read(address: int, cpu: int = 1, size: int = 4):
                    data = await call("read_memory", address=address, cpu=cpu,
                                      length=size, fmt=f"u{size * 8}")
                    return data["values"][0]

                async def snapshot():
                    return {"status": await call("get_status"),
                            "system": await call("get_system_info"),
                            "registers": [await call("read_registers", cpu=cpu)
                                          for cpu in (0, 1)]}

                assert not (await call("is_running"))["running"]
                await reject("get_rom_info")
                await reject("savestate_save", slot=1)
                await call("load_rom", path=str(rom))
                assert (await call("is_running"))["running"]
                assert (await call("advance_frames", frames=2))["frames_executed"] == 2
                assert await read(COUNTERS[0], cpu=0) > 0
                await call("pause_emulation")
                assert not (await call("is_running"))["running"]
                paused = await snapshot()
                assert (await call("advance_frames", frames=1))["frames_executed"] == 0
                assert await snapshot() == paused
                await call("resume_emulation")
                assert (await call("is_running"))["running"]
                assert (await call("advance_frames", frames=1))["frames_executed"] == 1
                await call("pause_emulation")

                # Status queries must not reset the native cycle counter or PC.
                stable = await snapshot()
                info = await call("get_rom_info")
                assert info["title"] == "MCP SMOKE" and info["code"] == "####", info
                assert info["rom_size"] == ROM_SIZE, info
                assert (info["arm9_entry"], info["arm7_entry"]) == CODE_BASES
                perf = await call("get_performance")
                assert perf["fps"] >= 0 and perf["emulation_speed"] >= 0, perf
                if perf["host"] is not None:
                    assert perf["host"]["memory_mb"] > 0 and perf["host"]["cpu_percent"] >= 0
                assert stable["system"]["screen"] == {"width": 256, "height": 192}
                assert stable["system"]["system_clock_cycles"] > 0
                assert stable["status"]["system_clock_cycles"] == stable["system"]["system_clock_cycles"]
                for cpu in (0, 1):
                    assert stable["status"][f"pc_arm{9 if cpu == 0 else 7}"] == stable["registers"][cpu]["instruction_address"]
                assert await snapshot() == stable
                evidence["nonmutating_status"] = {"cycles": stable["system"]["system_clock_cycles"],
                    "frames": stable["status"]["frames"], "rom": info, "performance": perf}

                # LDRH/STR loop samples KEYINPUT and EXTKEYIN while tools run frames.
                capture_code, capture_data = 0x02005000, 0x02006000
                words = (0xE59F0018, 0xE59F1018, 0xE1D020B0, 0xE5812000,
                         0xE1D030B6, 0xE5813004, 0xEAFFFFFA, 0xE1A00000,
                         0x04000130, capture_data)
                await call("code_patch", cpu=1, address=capture_code,
                           hex_data=struct.pack("<10I", *words).hex())
                await call("write_register", cpu=1, name="pc", value=capture_code)
                await call("set_buttons", buttons=["a", "x", "left"])
                held = await call("get_buttons")
                assert set(held["pressed"]) == {"a", "x", "left"} and held["mask"] == 0x421
                assert await read(0x04000130, size=2) == (0x3FF & ~0x21)
                assert await read(0x04000136, size=2) & 3 == 2
                await reject("set_buttons", buttons=["not-a-ds-button"])
                assert await call("get_buttons") == held
                await call("set_buttons", buttons=[])
                assert (await call("get_buttons"))["mask"] == 0
                await call("resume_emulation")
                pressed = await call("press_buttons", buttons=["b", "y", "r"], frames=2)
                assert pressed["frames_executed"] == 2, pressed
                captured_keys = await read(capture_data)
                captured_ext = await read(capture_data + 4)
                assert captured_keys == (0x3FF & ~0x102), hex(captured_keys)
                assert captured_ext & 3 == 1, hex(captured_ext)
                assert (await call("get_buttons"))["mask"] == 0
                assert await read(0x04000130, size=2) == 0x3FF
                evidence["press_buttons_guest_sample"] = {"keyinput": captured_keys,
                    "extkeyin": captured_ext, "frames": pressed["frames_executed"]}

                # src/SPI.cpp TSC::SetTouchCoords clears EXTKEYIN bit 6 on touch,
                # sets it on release; src/NDS.cpp sets bit 7 while the lid closes.
                await call("set_touch", x=127, y=95)
                assert await read(0x04000136, size=2) & 0x40 == 0
                await reject("set_touch", x=256, y=95)
                assert await read(0x04000136, size=2) & 0x40 == 0
                await call("release_touch")
                assert await read(0x04000136, size=2) & 0x40
                tapped = await call("tap_screen", x=255, y=191, frames=2)
                assert tapped["frames_executed"] == 2, tapped
                tapped_ext = await read(capture_data + 4)
                assert tapped_ext & 0x40 == 0, hex(tapped_ext)
                assert await read(0x04000136, size=2) & 0x40
                await call("set_lid", closed=True)
                assert await read(0x04000136, size=2) & 0x80
                await call("set_lid", closed=False)
                assert await read(0x04000136, size=2) & 0x80 == 0
                evidence["tap_screen_guest_sample"] = {"extkeyin": tapped_ext,
                    "frames": tapped["frames_executed"], "released_after_call": True}

                # Poll after changing held input: the first completed frame makes
                # the ARM7 sampler store the expected value into ordinary RAM.
                await call("set_buttons", buttons=["start"])
                target = 0x3FF & ~8
                polled = await call("advance_frames_until", address=capture_data,
                                    expected=target, size=4, cpu=1, max_frames=3)
                assert polled["match"] and polled["frames_executed"] == 1, polled
                immediate = await call("advance_frames_until", address=capture_data,
                                       expected=target, size=4, cpu=1, max_frames=3)
                assert immediate["match"] and immediate["frames_executed"] == 0, immediate
                capped = await call("advance_frames_until", address=capture_data,
                                    expected=0xFFFFFFFF, size=4, cpu=1, max_frames=2)
                assert not capped["match"] and capped["frames_executed"] == 2, capped
                await call("set_buttons", buttons=[])
                await call("pause_emulation")
                evidence["polling"] = {"became_match": polled, "already_match": immediate,
                                       "frame_limit": capped}

                # Decode every declared watch type from a known little-endian
                # fixture, then update storage and check live values refresh.
                watch_base = 0x02007000
                fixture = (b"\xfe" + b"\x00" + struct.pack("<H", 65000)
                           + struct.pack("<I", 0xDEADBEEF) + struct.pack("<h", -1234)
                           + b"\x00\x00" + struct.pack("<i", -1234567)
                           + struct.pack("<f", 1.25) + b"MCP watch\x00extra")
                await call("memory_poke", cpu=0, address=watch_base, hex_data=fixture.hex())
                watches = [("u8", 0, 254), ("s8", 0, -2), ("u16", 2, 65000),
                           ("u32", 4, 0xDEADBEEF), ("s16", 8, -1234),
                           ("s32", 12, -1234567), ("float", 16, 1.25),
                           ("ascii", 20, "MCP watch")]
                ids = []
                for kind, offset, expected in watches:
                    item = await call("watch_add", label=f"fixture_{kind}",
                                      address=watch_base + offset, wtype=kind, cpu=0, length=15)
                    ids.append(item["id"])
                metadata = await call("watch_list", read_values=False)
                assert len(metadata["watches"]) == 8
                assert all("value" not in item for item in metadata["watches"])
                values = await call("watch_list")
                assert {w["type"]: w["value"] for w in values["watches"]} == {
                    kind: value for kind, _, value in watches}, values
                assert (await call("get_status"))["debug"]["watches"] == 8
                await call("memory_poke", address=watch_base, hex_data="7f")
                refreshed = await call("watch_list")
                assert refreshed["watches"][0]["value"] == refreshed["watches"][1]["value"] == 127
                await call("watch_remove", watch_id=ids[0])
                await reject("watch_remove", watch_id=ids[0])
                assert (await call("watch_clear"))["removed"] == 7
                assert (await call("watch_list"))["watches"] == []
                evidence["watch_values"] = values["watches"]

                # A slot checkpoint must restore actual RAM, registers, frame
                # and clock state, not merely report a successfully opened file.
                checkpoint = await snapshot()
                saved = await call("savestate_save", slot=3)
                slot_file = rom.with_suffix(".slot3.mst")
                assert Path(saved["path"]) == slot_file and slot_file.stat().st_size > 1_000_000
                await call("memory_poke", address=watch_base, hex_data="00112233")
                await call("resume_emulation")
                await call("advance_frames", frames=2)
                await call("pause_emulation")
                await call("savestate_load", slot=3)
                restored = await snapshot()
                # Loading restores the guest state; the Python performance
                # rolling window intentionally remains a host statistic.
                for key in ("system_clock_cycles", "frames", "pc_arm9", "pc_arm7"):
                    assert restored["status"][key] == checkpoint["status"][key], (key, restored, checkpoint)
                assert restored["registers"] == checkpoint["registers"]
                assert await read(watch_base, cpu=0) == int.from_bytes(b"\x7f\x00\xe8\xfd", "little")
                failure_before = await snapshot()
                await reject("savestate_load", slot=9)
                await reject("savestate_save", slot=0)
                await reject("savestate_load", slot=3, path=str(slot_file))
                await reject("savestate_save", path=str(temporary))
                corrupt = temporary / "invalid.mst"
                corrupt.write_bytes(b"this is not a melonDS savestate")
                await reject("savestate_load", path=str(corrupt))
                assert await snapshot() == failure_before
                evidence["savestate_slot"] = {"slot": 3, "bytes": slot_file.stat().st_size,
                    "restored_ram_registers_frame_clock": True, "failed_requests_preserve_state": True}

                await call("reset_emulation")
                reset = await call("get_status")
                assert reset["running"] and reset["frames"] == 0, reset
                assert (reset["pc_arm9"], reset["pc_arm7"]) == CODE_BASES, reset
                assert await read(watch_base, cpu=0) == 0
                assert await read(COUNTERS[0], cpu=0) == 0
                assert (await call("advance_frames", frames=1))["frames_executed"] == 1
                assert await read(COUNTERS[0], cpu=0) > 0
                assert await read(COUNTERS[1], cpu=1) > 0
                evidence["reset"] = {"initial_pcs": [reset["pc_arm9"], reset["pc_arm7"]],
                                     "frame_zero": True, "both_guest_loops_restarted": True}
                result = {"backend": "real_native_dll_via_mcp_stdio",
                    "library": str(library.resolve()), "calls": len(completed),
                    "verified_tools": sorted(set(completed)), "rejected_calls": len(rejected),
                    "rejected_tools": sorted(set(rejected)), "evidence": evidence,
                    "rom": "self-authored ARM9/ARM7 counter and input-sampling loops"}
                (artifacts / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
                return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, default=Path("build/control-workflow-e2e"))
    args = parser.parse_args()
    result = asyncio.run(asyncio.wait_for(workflow(args.library, args.artifacts), timeout=120))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
