"""Real MCP rendering of text screenblocks and individual OAM sprites.

Known patterns are placed through emulated bus writes. Pixel expectations below
are independent of the production decoders. Every inspection is non-mutating.
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

COLORS = [(0, 0, 0, 0), (255, 0, 0, 255), (0, 255, 0, 255),
          (0, 0, 255, 255), (255, 255, 0, 255)]


async def workflow(library: Path, artifacts: Path) -> dict:
    root = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    environment.update(PYTHONPATH=str(root / "mcp/python"), MELONDS_MCP_LIB=str(library.resolve()))
    environment.pop("MELONDS_MCP_ROM", None)
    params = StdioServerParameters(command=sys.executable, args=["-m", "melonds_mcp"],
                                  env=environment, cwd=str(root))
    artifacts.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="melonds-maps-") as temp:
        rom = write_rom(Path(temp) / "maps.nds")
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                calls = []
                captures = []
                async def call(name, **arguments):
                    result = await session.call_tool(name, arguments)
                    assert not result.isError, (name, result.content)
                    data = result.structuredContent
                    if data is None:
                        data = json.loads(next(c.text for c in result.content if c.type == "text"))
                    assert data.get("ok", True), (name, data)
                    calls.append(name)
                    return data, result

                async def write(address, value, size=4):
                    await call("write_memory", address=address, value=value, size=size)

                async def block(address, payload):
                    assert len(payload) % 4 == 0
                    for index, (word,) in enumerate(struct.iter_unpack("<I", payload)):
                        await write(address + index * 4, word)

                async def capture(name, filename, **arguments):
                    before = (await call("gpu_state"))[0]
                    registers = [(await call("read_registers", cpu=cpu))[0] for cpu in (0, 1)]
                    meta, result = await call(name, **arguments)
                    after = (await call("gpu_state"))[0]
                    registers_after = [(await call("read_registers", cpu=cpu))[0] for cpu in (0, 1)]
                    assert before == after and registers == registers_after
                    assert meta["frame_number"] == before["frame_number"]
                    assert meta["vcount"] == before["vcount"]
                    images = [item for item in result.content if item.type == "image"]
                    assert len(images) == 1 and images[0].mimeType == "image/png"
                    png = base64.b64decode(images[0].data, validate=True)
                    image = Image.open(io.BytesIO(png)).convert("RGBA")
                    assert meta["pixel_format"] == "RGBA8"
                    assert hashlib.sha256(png).hexdigest() == meta["png_sha256"]
                    assert hashlib.sha256(image.tobytes()).hexdigest() == meta["pixels_sha256"]
                    (artifacts / filename).write_bytes(png)
                    captures.append({"tool": name, "file": filename, "metadata": meta})
                    return image

                await call("load_rom", path=str(rom))
                await call("advance_frames", frames=2)
                await call("pause_emulation")
                await write(0x04000304, 0x820F, 2)
                await write(0x04000000, 0x00010000)
                await write(0x04000240, 0x80, 1)  # VRAM A -> LCDC for fixture writes.
                for palette in (0x05000000, 0x05000200):
                    for index, color in enumerate((0, 0x001F, 0x03E0, 0x7C00, 0x03FF)):
                        await write(palette + index * 2, color, 2)
                # Map source starts at +0x8000; reset must provide a known empty map.
                for offset in (0x8000, 0x9000):
                    data, _ = await call("gpu_read_vram", bank="A", offset=offset, length=4096)
                    assert data["hex"] == "00" * 4096
                # Tile 0: alternating red/transparent, catches low-nibble/alpha handling.
                tile0 = bytes(0x01 if y % 2 == 0 else 0x10 for y in range(8) for x in range(4))
                tile1 = bytes(0x21 if y < 4 else 0x43 for y in range(8) for x in range(4))
                await block(0x06800000, tile0 + tile1 + b"\x33" * 32 + b"\x44" * 32)
                # First entry in each of the four 32x32 screenblocks differs.
                for index, entry in enumerate((1, 2, 3, 1 | 0xC00)):
                    await write(0x06808000 + index * 2048, entry, 2)
                tilemap = await capture("gpu_tilemap", "text-map-64x64.png", map_bank="A",
                                        map_offset=0x8000, tile_bank="A", tile_offset=0,
                                        bpp=4, map_width=64, map_height=64)
                assert tilemap.size == (512, 512)
                for y in range(512):
                    for x in range(512):
                        tx, ty, px, py = x // 8, y // 8, x % 8, y % 8
                        if (tx, ty) == (0, 0):
                            color = 1 + px % 2 if py < 4 else 3 + px % 2
                        elif (tx, ty) == (32, 0):
                            color = 3
                        elif (tx, ty) == (0, 32):
                            color = 4
                        elif (tx, ty) == (32, 32):
                            sx, sy = 7 - px, 7 - py
                            color = 1 + sx % 2 if sy < 4 else 3 + sx % 2
                        else:
                            color = 1 if (px + py) % 2 == 0 else 0
                        assert tilemap.getpixel((x, y)) == COLORS[color], (x, y, color)

                sprite_source = 0x06801000
                await block(sprite_source, b"\x11" * 32 + b"\x22" * 32)
                await block(sprite_source + 1024, b"\x33" * 32 + b"\x00" * 32)
                await write(0x07000000, 0, 2)
                await write(0x07000002, 0x5000, 2)  # 16x16, H flip.
                await write(0x07000004, 0, 2)
                sprite = await capture("gpu_sprite", "sprite-2d-flipped.png", engine="A",
                                       index=0, bank="A", offset=0x1000)
                assert sprite.size == (16, 16)
                for y in range(16):
                    for x in range(16):
                        color = (2 if x < 8 else 1) if y < 8 else (0 if x < 8 else 3)
                        assert sprite.getpixel((x, y)) == COLORS[color]

                # Identity affine with doubled bounds: source centered in 32x32.
                await write(0x07000000, 0x300, 2)
                await write(0x07000002, 0x4000, 2)
                for offset, coefficient in zip((6, 14, 22, 30), (256, 0, 0, 256)):
                    await write(0x07000000 + offset, coefficient, 2)
                affine = await capture("gpu_sprite", "sprite-affine-double.png", engine="A",
                                       index=0, bank="A", offset=0x1000)
                assert affine.size == (32, 32)
                for y in range(32):
                    for x in range(32):
                        sx, sy = x - 8, y - 8
                        color = 0
                        if 0 <= sx < 16 and 0 <= sy < 16:
                            color = (1 if sx < 8 else 2) if sy < 8 else (3 if sx < 8 else 0)
                        assert affine.getpixel((x, y)) == COLORS[color]

                # Same sprite in tightly packed 1D mode, no flip.
                await write(0x04000000, 0x00010010)
                await write(0x07000000, 0, 2)
                await write(0x07000002, 0x4000, 2)
                await block(sprite_source + 64, b"\x33" * 32 + b"\x00" * 32)
                packed = await capture("gpu_sprite", "sprite-1d.png", engine="A",
                                       index=0, bank="A", offset=0x1000)
                for y in range(16):
                    for x in range(16):
                        color = (1 if x < 8 else 2) if y < 8 else (3 if x < 8 else 0)
                        assert packed.getpixel((x, y)) == COLORS[color]
                await write(0x07000000, 0xC00, 2)  # Unsupported bitmap, never fake tiled output.
                invalid = await session.call_tool("gpu_sprite", {"engine": "A", "index": 0, "bank": "A", "offset": 0x1000})
                assert invalid.isError
                report = {"backend": "real_native_graphics_via_mcp_stdio", "calls": len(calls),
                          "verified_tools": sorted(set(calls)), "captures": captures,
                          "all_pixels_verified": True, "inspection_state_unchanged": True,
                          "fixture": "original text screenblocks and 4bpp sprite patterns"}
                (artifacts / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
                return {key: value for key, value in report.items() if key != "captures"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, default=Path("build/graphics-maps-e2e"))
    args = parser.parse_args()
    result = asyncio.run(asyncio.wait_for(workflow(args.library, args.artifacts), timeout=90))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
