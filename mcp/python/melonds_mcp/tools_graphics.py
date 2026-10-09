"""Read-only physical GPU inspection: no MMIO and no emulation advancement."""

from __future__ import annotations

import base64
import ctypes
import hashlib
import io
import json
from typing import Annotated, Literal

from mcp.types import CallToolResult, ImageContent, TextContent
from PIL import Image
from pydantic import Field, StrictInt

from .graphics import (
    bounded_int, decode_oam, decode_palette, decode_state, palette_pixels,
    tile_pixels, vram_range,
    tilemap_pixels, tilemap_source_length, sprite_pixels, sprite_source_layout,
)

Bank = Literal["A", "B", "C", "D", "E", "F", "G", "H", "I"]
Engine = Literal["A", "B"]
PaletteKind = Literal["bg", "obj"]
Offset = Annotated[StrictInt, Field(ge=0, le=131071)]
Length = Annotated[StrictInt, Field(ge=1, le=4096)]
PaletteIndex = Annotated[StrictInt, Field(ge=0, le=15)]
TileCount = Annotated[StrictInt, Field(ge=1, le=256)]
Columns = Annotated[StrictInt, Field(ge=1, le=32)]
SpriteIndex = Annotated[StrictInt, Field(ge=0, le=127)]


def _engine(engine: str) -> int:
    if engine not in ("A", "B"):
        raise ValueError("engine must be A or B")
    return 0 if engine == "A" else 1


def _palette_offset(kind: str) -> int:
    if kind not in ("bg", "obj"):
        raise ValueError("kind must be bg or obj")
    return 0 if kind == "bg" else 512


def _read(lib, region: int, bank: int, offset: int, length: int) -> bytes:
    buffer = (ctypes.c_ubyte * length)()
    copied = lib.melonds_gpu_read(region, bank, offset, buffer, length)
    if copied != length:
        raise RuntimeError(f"native GPU read failed: wanted {length}, received {copied}")
    return bytes(buffer)


def _state(lib) -> dict:
    words = (ctypes.c_uint32 * 16)()
    if lib.melonds_gpu_state(words, 16) != 16:
        raise RuntimeError("native GPU state unavailable")
    return decode_state(list(words))


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _image_result(width: int, height: int, pixels: bytes, metadata: dict, mode: str = "RGB") -> CallToolResult:
    output = io.BytesIO()
    Image.frombytes(mode, (width, height), pixels).save(output, format="PNG")
    png = output.getvalue()
    metadata = {**metadata, "size": {"width": width, "height": height},
                "pixel_format": "RGBA8" if mode == "RGBA" else "RGB8", "pixels_sha256": _hash(pixels),
                "png_sha256": _hash(png)}
    return CallToolResult(content=[
        TextContent(type="text", text=json.dumps(metadata, separators=(",", ":"))),
        ImageContent(type="image", mimeType="image/png", data=base64.b64encode(png).decode("ascii")),
    ], structuredContent=metadata)


def register(mcp, emu) -> None:
    lib = emu.lib.lib

    @mcp.tool()
    def gpu_state() -> dict:
        """Inspect frame/VCOUNT, both DISPCNT, POWCNT1 and physical VRAM bank controls without stepping."""
        return {"ok": True, "access": "direct_gpu_copy", **_state(lib)}

    @mcp.tool()
    def gpu_read_vram(bank: Bank, offset: Offset = 0, length: Length = 256) -> dict:
        """Read up to 4096 physical VRAM bytes from bank A-I (not a CPU mapped address)."""
        bounded_int(length, 1, 4096, "length")
        index = vram_range(bank, offset, length)
        state = _state(lib)
        data = _read(lib, 0, index, offset, length)
        return {"ok": True, "bank": bank, "offset": offset, "length": length,
                "hex": data.hex(), "sha256": _hash(data), "access": "direct_gpu_copy",
                "frame_number": state["frame_number"], "vcount": state["vcount"]}

    @mcp.tool()
    def gpu_palette(engine: Engine = "A", kind: PaletteKind = "bg") -> CallToolResult:
        """View 256 standard BG/OBJ palette colors as a 16x16 swatch image and RGB555 entries; not extended palettes."""
        engine_index, offset = _engine(engine), _palette_offset(kind)
        state = _state(lib)
        data = _read(lib, 1, engine_index, offset, 512)
        width, height, pixels = palette_pixels(data)
        return _image_result(width, height, pixels, {
            "ok": True, "engine": engine, "kind": kind, "palette_type": "standard",
            "entry_count": 256, "columns": 16, "cell_size": 12,
            "entries": decode_palette(data), "source_sha256": _hash(data),
            "frame_number": state["frame_number"], "vcount": state["vcount"],
            "access": "direct_gpu_copy", "bit15": "reported separately; ignored for RGB swatch",
        })

    @mcp.tool()
    def gpu_tiles(bank: Bank, offset: Offset = 0, bpp: Literal[4, 8] = 4,
                  palette_engine: Engine = "A", palette_kind: PaletteKind = "bg",
                  palette_index: PaletteIndex = 0, tile_count: TileCount = 64,
                  columns: Columns = 8) -> CallToolResult:
        """Decode consecutive physical VRAM bytes as raw 8x8 4bpp/8bpp tiles; not a tilemap or composed screen."""
        if type(bpp) is not int or bpp not in (4, 8):
            raise ValueError("bpp must be 4 or 8")
        bounded_int(tile_count, 1, 256, "tile_count")
        bounded_int(columns, 1, 32, "columns")
        bounded_int(palette_index, 0, 15, "palette_index")
        if bpp == 8 and palette_index != 0:
            raise ValueError("8bpp palette_index must be 0")
        length = tile_count * 8 * bpp
        bank_index = vram_range(bank, offset, length)
        palette_bank, palette_offset = _engine(palette_engine), _palette_offset(palette_kind)
        state = _state(lib)
        data = _read(lib, 0, bank_index, offset, length)
        palette = _read(lib, 1, palette_bank, palette_offset, 512)
        width, height, pixels = tile_pixels(data, palette, bpp, palette_index, tile_count, columns)
        return _image_result(width, height, pixels, {
            "ok": True, "view": "raw_tile_sheet", "bank": bank, "offset": offset,
            "length": length, "bpp": bpp, "tile_width": 8, "tile_height": 8,
            "tile_count": tile_count, "columns": columns,
            "palette_engine": palette_engine, "palette_kind": palette_kind,
            "palette_index": palette_index, "palette_type": "standard",
            "source_sha256": _hash(data), "palette_sha256": _hash(palette),
            "frame_number": state["frame_number"], "vcount": state["vcount"],
            "access": "direct_gpu_copy", "index_zero": "displayed as palette RGB; transparency is not composed",
            "unused_sheet_cells": "black", "composed": False,
        })

    @mcp.tool()
    def gpu_oam(engine: Engine = "A") -> dict:
        """Decode 128 OAM entries, wrapped coordinates, sprite attributes and signed 8.8 affine matrices; not rendered sprites."""
        engine_index = _engine(engine)
        state = _state(lib)
        data = _read(lib, 2, engine_index, 0, 1024)
        entries = decode_oam(data)
        return {"ok": True, "engine": engine, "entry_count": 128,
                "enabled_count": sum(not entry["disabled"] for entry in entries),
                "entries": entries, "source_sha256": _hash(data),
                "frame_number": state["frame_number"], "vcount": state["vcount"],
                "access": "direct_gpu_copy", "composed": False,
                "notes": ["x is signed 9-bit; y wraps modulo 256 and rows 128-191 remain positive",
                          "visible_y_ranges are half-open geometric intervals, not a visibility/compositing guarantee",
                          "tile_index is raw; actual address depends on engine mapping and bitmap mode",
                          "attr word 3 stores a shared affine parameter, not an independent sprite attribute"]}

    @mcp.tool()
    def gpu_tilemap(map_bank: Bank, map_offset: Offset, tile_bank: Bank, tile_offset: Offset,
                    bpp: Literal[4, 8] = 4, palette_engine: Engine = "A",
                    map_width: Literal[32, 64] = 32,
                    map_height: Literal[32, 64] = 32) -> CallToolResult:
        """Render a raw DS text BG map from explicit physical map/tile banks using 32x32 screenblocks, flips and standard palettes; not a final screen."""
        if type(bpp) is not int or bpp not in (4, 8):
            raise ValueError("bpp must be 4 or 8")
        if type(map_width) is not int or map_width not in (32, 64):
            raise ValueError("map_width must be 32 or 64")
        if type(map_height) is not int or map_height not in (32, 64):
            raise ValueError("map_height must be 32 or 64")
        map_length = map_width * map_height * 2
        map_index = vram_range(map_bank, map_offset, map_length)
        tile_index = vram_range(tile_bank, tile_offset, 8 * bpp)
        engine_index = _engine(palette_engine)
        state = _state(lib)
        if bpp == 8 and state["dispcnt"][palette_engine] & (1 << 30):
            raise ValueError("extended BG palettes are not supported by this standard-palette text map view")
        map_data = _read(lib, 0, map_index, map_offset, map_length)
        tile_length = tilemap_source_length(map_data, bpp, map_width, map_height)
        vram_range(tile_bank, tile_offset, tile_length)
        tiles = _read(lib, 0, tile_index, tile_offset, tile_length)
        palette = _read(lib, 1, engine_index, 0, 512)
        width, height, pixels = tilemap_pixels(map_data, tiles, palette, bpp, map_width, map_height)
        return _image_result(width, height, pixels, {
            "ok": True, "view": "raw_text_bg_tilemap", "map_bank": map_bank,
            "map_offset": map_offset, "map_length": map_length,
            "tile_bank": tile_bank, "tile_offset": tile_offset, "tile_length": tile_length,
            "bpp": bpp, "map_width": map_width, "map_height": map_height,
            "palette_engine": palette_engine, "palette_type": "standard_bg",
            "screenblock_size_tiles": 32, "screenblock_order": "row_major",
            "map_sha256": _hash(map_data), "tile_sha256": _hash(tiles),
            "palette_sha256": _hash(palette), "frame_number": state["frame_number"],
            "vcount": state["vcount"], "access": "direct_gpu_copy", "composed": False,
            "index_zero": "transparent", "notes": [
                "physical source addresses are explicit; bank mapping is not inferred",
                "no scrolling, mosaic, window, priority, brightness or screen composition",
                "8bpp map palette-bank bits are ignored with standard BG palettes",
            ],
        }, mode="RGBA")

    @mcp.tool()
    def gpu_sprite(engine: Engine, index: SpriteIndex, bank: Bank,
                   offset: Offset = 0) -> CallToolResult:
        """Inspect one OAM sprite from an explicit physical source start; use DISPCNT 1D/2D stride, standard OBJ palette, flips and affine sampling."""
        engine_index = _engine(engine)
        bounded_int(index, 0, 127, "index")
        bank_index = vram_range(bank, offset, 1)
        state = _state(lib)
        oam = _read(lib, 2, engine_index, 0, 1024)
        sprite = decode_oam(oam)[index]
        dispcnt = state["dispcnt"][engine]
        layout = sprite_source_layout(sprite, dispcnt)
        vram_range(bank, offset, layout["length"])
        data = _read(lib, 0, bank_index, offset, layout["length"])
        palette = _read(lib, 1, engine_index, 512, 512)
        width, height, pixels = sprite_pixels(data, palette, sprite, dispcnt)
        return _image_result(width, height, pixels, {
            "ok": True, "view": "single_sprite", "engine": engine, "index": index,
            "bank": bank, "offset": offset, **layout, "sprite": sprite,
            "palette_type": "standard_obj", "dispcnt": dispcnt,
            "source_sha256": _hash(data), "oam_sha256": _hash(oam),
            "palette_sha256": _hash(palette), "frame_number": state["frame_number"],
            "vcount": state["vcount"], "access": "direct_gpu_copy", "composed": False,
            "index_zero": "transparent", "mosaic_applied": False,
            "window_mask": sprite["mode"] == "obj_window", "notes": [
                "offset is the already resolved sprite source start; OAM tile_index is not added again",
                "DISPCNT 1D boundary bits affect the base only; the source offset already accounts for them",
                "image uses local sprite bounds, without screen clipping, mosaic, windows, priorities or background blending",
                "semitransparent OBJ colors are shown opaque before composition; OBJ-window is a white coverage mask",
            ],
        }, mode="RGBA")
