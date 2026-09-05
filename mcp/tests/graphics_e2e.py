"""Actual MCP stdio -> native GPU storage -> decoded image/attributes.

The fixture writes only intentional emulated GPU registers/RAM. Every inspection
must leave both CPUs and the frame/VCOUNT snapshot unchanged. No external ROM.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from PIL import Image

from synthetic_rom import write_rom


async def workflow(library: Path, artifacts: Path) -> dict:
    root = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    environment.update(PYTHONPATH=str(root / "mcp/python"),
                       MELONDS_MCP_LIB=str(library.resolve()))
    environment.pop("MELONDS_MCP_ROM", None)
    params = StdioServerParameters(command=sys.executable,
                                  args=["-m", "melonds_mcp"],
                                  env=environment, cwd=str(root))
    artifacts.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="melonds-gpu-e2e-") as temp:
        rom = write_rom(Path(temp) / "gpu.nds")
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                required = {"gpu_state", "gpu_read_vram", "gpu_palette", "gpu_tiles", "gpu_oam"}
                listed = await session.list_tools()
                assert required <= {tool.name for tool in listed.tools}
                calls = []

                async def call(name, **arguments):
                    result = await session.call_tool(name, arguments)
                    assert not result.isError, (name, result.content)
                    data = result.structuredContent
                    if data is None:
                        data = json.loads(next(c.text for c in result.content if c.type == "text"))
                    assert data.get("ok", True), (name, data)
                    calls.append(name)
                    return data, result

                def png_result(result, metadata, filename):
                    images = [item for item in result.content if item.type == "image"]
                    assert len(images) == 1 and images[0].mimeType == "image/png"
                    png = base64.b64decode(images[0].data, validate=True)
                    assert hashlib.sha256(png).hexdigest() == metadata["png_sha256"]
                    image = Image.open(io.BytesIO(png)).convert("RGB")
                    assert hashlib.sha256(image.tobytes()).hexdigest() == metadata["pixels_sha256"]
                    (artifacts / filename).write_bytes(png)
                    return image

                await call("load_rom", path=str(rom))
                await call("advance_frames", frames=2)
                await call("pause_emulation")
                await call("write_memory", address=0x04000304, value=0x820F, size=2)
                await call("write_memory", address=0x04000240, value=0x80, size=1)
                # BG and OBJ palettes are separate halves in each engine.
                for palette_base in (0x05000000, 0x05000200, 0x05000400, 0x05000600):
                    for index, color in enumerate((0, 0x001F, 0x03E0, 0x7C00)):
                        await call("write_memory", address=palette_base + index * 2,
                                   value=color, size=2)

                tile4 = bytes(0x21 if y % 2 == 0 else 0x12 for y in range(8) for x in range(4))
                tile8 = bytes(3 if (x + y) % 2 else 1 for y in range(8) for x in range(8))
                for offset, payload in ((0, tile4), (64, tile8)):
                    for word_index, (word,) in enumerate(struct.iter_unpack("<I", payload)):
                        await call("write_memory", address=0x06800000 + offset + word_index * 4,
                                   value=word, size=4)
                for offset, value in enumerate((0x00F8, 0x11FC, 0x2003, 0x0100)):
                    await call("write_memory", address=0x07000000 + offset * 2, value=value, size=2)
                # The physical bank must remain readable after CPU unmapping.
                await call("write_memory", address=0x04000240, value=0, size=1)
                before, _ = await call("gpu_state")
                regs_before = [(await call("read_registers", cpu=cpu))[0] for cpu in (0, 1)]
                raw, _ = await call("gpu_read_vram", bank="A", offset=0, length=32)
                assert raw["hex"] == tile4.hex()
                assert raw["sha256"] == hashlib.sha256(tile4).hexdigest()

                for engine in ("A", "B"):
                    for kind in ("bg", "obj"):
                        palette, response = await call("gpu_palette", engine=engine, kind=kind)
                        assert [entry["rgb"] for entry in palette["entries"][:4]] == [
                            [0, 0, 0], [255, 0, 0], [0, 255, 0], [0, 0, 255]]
                        swatches = png_result(response, palette, f"palette-{engine}-{kind}.png")
                        assert swatches.size == (192, 192)
                        assert swatches.getpixel((18, 6)) == (255, 0, 0)

                sheets = []
                for bpp, offset in ((4, 0), (8, 64)):
                    metadata, response = await call("gpu_tiles", bank="A", offset=offset,
                                                    bpp=bpp, tile_count=1, columns=1)
                    image = png_result(response, metadata, f"tiles-{bpp}bpp.png")
                    assert image.size == (8, 8)
                    for y in range(8):
                        for x in range(8):
                            expected = ((255, 0, 0) if (x + y) % 2 == 0
                                        else (0, 255, 0) if bpp == 4 else (0, 0, 255))
                            assert image.getpixel((x, y)) == expected, (bpp, x, y)
                    sheets.append(metadata)
                oam, _ = await call("gpu_oam", engine="A")
                sprite = oam["entries"][0]
                assert oam["entry_count"] == 128
                assert (sprite["x"], sprite["y"], sprite["width"], sprite["height"]) == (-4, -8, 8, 8)
                assert sprite["hflip"] and sprite["tile_index"] == 3 and sprite["palette_bank"] == 2
                invalid = await session.call_tool("gpu_read_vram", {"bank": "I", "offset": 16383, "length": 2})
                assert invalid.isError, "cross-bank read must fail, not silently clip"
                after, _ = await call("gpu_state")
                regs_after = [(await call("read_registers", cpu=cpu))[0] for cpu in (0, 1)]
                assert before == after and regs_before == regs_after
                report = {"backend": "real_native_gpu_via_mcp_stdio", "calls": len(calls),
                          "verified_tools": sorted(set(calls)), "state_unchanged": True,
                          "cpu_registers_unchanged": True, "unmapped_vram_read": True,
                          "frame_number": after["frame_number"], "tile_sheets": sheets,
                          "first_sprite": sprite, "rom": "generated homebrew"}
                (artifacts / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
                return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, default=Path("build/graphics-e2e"))
    args = parser.parse_args()
    result = asyncio.run(asyncio.wait_for(workflow(args.library, args.artifacts), timeout=60))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
