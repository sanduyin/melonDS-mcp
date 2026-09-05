"""Real MCP stdio -> ctypes -> melonDS regression using our synthetic ROM.

Run with mcp/.venv/Scripts/python.exe mcp/tests/mcp_e2e.py --library DLL.
No commercial ROM, firmware dump, GUI session, or network service is required.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
from pathlib import Path
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from PIL import Image

from synthetic_rom import CODE_BASES, COUNTERS, write_rom


async def workflow(library: Path, artifacts: Path) -> dict:
    root = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(root / "mcp" / "python")
    environment["MELONDS_MCP_LIB"] = str(library.resolve())
    environment.pop("MELONDS_MCP_ROM", None)
    server = StdioServerParameters(
        command=sys.executable, args=["-m", "melonds_mcp"], env=environment,
        cwd=str(root),
    )
    artifacts.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="melonds-mcp-e2e-") as temp:
        temporary = Path(temp)
        rom = write_rom(temporary / "synthetic.nds")
        async with stdio_client(server) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                listed = await session.list_tools()
                assert len(listed.tools) >= 48, len(listed.tools)
                assert {"load_rom", "advance_frames", "read_memory", "write_memory",
                        "savestate_save", "savestate_load", "breakpoint_add",
                        "run_until_break", "read_registers", "step", "screenshot"} <= {
                            tool.name for tool in listed.tools}
                completed = []

                async def call(name: str, **arguments):
                    result = await session.call_tool(name, arguments)
                    assert not result.isError, (name, result.content)
                    data = result.structuredContent
                    if data is None:
                        texts = [item.text for item in result.content if item.type == "text"]
                        data = json.loads(texts[0]) if texts else {}
                    assert data.get("ok", True), (name, data)
                    completed.append(name)
                    return data, result

                await call("load_rom", path=str(rom))
                advanced, _ = await call("advance_frames", frames=3)
                assert advanced["frames_executed"] == 3, advanced
                for cpu, address in enumerate(COUNTERS):
                    memory, _ = await call("read_memory", cpu=cpu, address=address,
                                           length=4, fmt="u32")
                    assert memory["values"][0] > 0, memory

                scratch = 0x02003000
                await call("write_memory", cpu=0, address=scratch, size=4,
                           value=0x12345678)
                before, _ = await call("read_memory", cpu=0, address=scratch,
                                       length=4, fmt="hex")
                saved = temporary / "checkpoint.mst"
                await call("savestate_save", path=str(saved))
                await call("write_memory", cpu=0, address=scratch, size=4, value=99)
                await call("savestate_load", path=str(saved))
                after, _ = await call("read_memory", cpu=0, address=scratch,
                                      length=4, fmt="hex")
                assert before == after, (before, after)

                bp, _ = await call("breakpoint_add", cpu=1, address=CODE_BASES[1] + 8)
                stopped, _ = await call("run_until_break", max_frames=2)
                assert stopped["break_hit"], stopped
                regs1, _ = await call("read_registers", cpu=1)
                regs2, _ = await call("read_registers", cpu=1)
                assert regs1 == regs2, (regs1, regs2)
                stepped, _ = await call("step", cpu=1, count=1)
                assert stepped["hit"], stepped
                await call("breakpoint_clear", cpu=-1)
                await call("continue_after_break")

                # Explicitly program 2D backdrop colors, then render a frame.
                # These are intentional bus writes to emulated display registers.
                await call("write_memory", cpu=0, address=0x04000304, size=2,
                           value=0x820F)
                for address in (0x04000000, 0x04001000):
                    await call("write_memory", cpu=0, address=address, size=4,
                               value=0x00010000)
                for address, color in ((0x05000000, 0x001F), (0x05000400, 0x7C00)):
                    await call("write_memory", cpu=0, address=address, size=2,
                               value=color)
                await call("advance_frames", frames=2)
                metadata, captured = await call("screenshot", screen="both", format="png")
                images = [item for item in captured.content if item.type == "image"]
                assert len(images) == 1, captured.content
                assert images[0].mimeType == "image/png"
                png = base64.b64decode(images[0].data, validate=True)
                with Image.open(io.BytesIO(png)) as image:
                    assert image.size == (256, 384), image.size
                    colors = {image.convert("RGB").getpixel((128, y)) for y in (96, 288)}
                    # melonDS expands BGR555 through its 6-bit renderer: the
                    # maximum 5-bit component becomes 62, then 251 in RGB8.
                    assert colors == {(251, 0, 0), (0, 0, 251)}, colors
                (artifacts / "dual-screen.png").write_bytes(png)
                result = {
                    "backend": "real_native_dll_via_mcp_stdio",
                    "library": str(library.resolve()), "tool_count": len(listed.tools),
                    "calls": len(completed), "verified_tools": sorted(set(completed)),
                    "screenshot": metadata, "dual_screen_colors": sorted(colors),
                    "rom": "generated ARM9/ARM7 counter loops; no commercial data",
                }
                (artifacts / "result.json").write_text(
                    json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, default=Path("build/mcp-e2e"))
    args = parser.parse_args()
    result = asyncio.run(asyncio.wait_for(workflow(args.library, args.artifacts), timeout=60))
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
