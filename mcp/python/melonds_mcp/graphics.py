"""Pure DS palette/tile/OAM decoders, with no emulator execution or bus reads.

OAM bit fields and geometry follow melonDS src/GPU2D_Soft.cpp DrawSprites,
DrawSprite_Normal and DrawSprite_Rotscale. These are inspection views, not a
replacement for the renderer's bank mapping, blending or compositing rules.
"""

from __future__ import annotations

import struct


VRAM_BANKS = tuple("ABCDEFGHI")
VRAM_SIZES = (128 * 1024, 128 * 1024, 128 * 1024, 128 * 1024,
              64 * 1024, 16 * 1024, 16 * 1024, 32 * 1024, 16 * 1024)
SPRITE_SIZES = (
    ((8, 8), (16, 16), (32, 32), (64, 64)),
    ((16, 8), (32, 8), (32, 16), (64, 32)),
    ((8, 16), (8, 32), (16, 32), (32, 64)),
)


def bounded_int(value: int, minimum: int, maximum: int, name: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in [{minimum}, {maximum}]")
    return value


def vram_range(bank: str, offset: int, length: int) -> int:
    if bank not in VRAM_BANKS:
        raise ValueError("bank must be A-I")
    index = VRAM_BANKS.index(bank)
    bounded_int(offset, 0, VRAM_SIZES[index] - 1, "offset")
    bounded_int(length, 1, VRAM_SIZES[index], "length")
    if offset + length > VRAM_SIZES[index]:
        raise ValueError(f"range exceeds physical VRAM bank {bank}")
    return index


def rgb555(value: int) -> tuple[int, int, int]:
    """DS little-endian color: low 5 bits red; bit 15 is not RGB."""
    bounded_int(value, 0, 65535, "color")
    components = ((value >> shift) & 31 for shift in (0, 5, 10))
    return tuple((v << 3) | (v >> 2) for v in components)


def decode_palette(data: bytes) -> list[dict]:
    if len(data) != 512:
        raise ValueError("a standard BG/OBJ palette must contain 512 bytes")
    return [
        {"index": index, "raw": value, "rgb": list(rgb555(value)),
         "hex": "#%02x%02x%02x" % rgb555(value), "bit15": bool(value & 0x8000)}
        for index, (value,) in enumerate(struct.iter_unpack("<H", data))
    ]


def palette_pixels(data: bytes, cell_size: int = 12) -> tuple[int, int, bytes]:
    bounded_int(cell_size, 1, 32, "cell_size")
    colors = [tuple(entry["rgb"]) for entry in decode_palette(data)]
    side = 16 * cell_size
    pixels = bytearray(side * side * 3)
    for y in range(side):
        for x in range(side):
            color = colors[(y // cell_size) * 16 + x // cell_size]
            pos = (y * side + x) * 3
            pixels[pos:pos + 3] = bytes(color)
    return side, side, bytes(pixels)


def tile_pixels(data: bytes, palette: bytes, bpp: int, palette_index: int,
                tile_count: int, columns: int) -> tuple[int, int, bytes]:
    if type(bpp) is not int or bpp not in (4, 8):
        raise ValueError("bpp must be 4 or 8")
    bounded_int(palette_index, 0, 15, "palette_index")
    bounded_int(tile_count, 1, 256, "tile_count")
    bounded_int(columns, 1, 32, "columns")
    if bpp == 8 and palette_index != 0:
        raise ValueError("8bpp uses all 256 colors; palette_index must be 0")
    tile_bytes = 8 * bpp
    if len(data) != tile_count * tile_bytes:
        raise ValueError("tile byte length does not match tile_count and bpp")
    colors = [bytes(entry["rgb"]) for entry in decode_palette(palette)]
    width, height = columns * 8, ((tile_count + columns - 1) // columns) * 8
    pixels = bytearray(width * height * 3)
    for tile in range(tile_count):
        tile_x, tile_y = (tile % columns) * 8, (tile // columns) * 8
        for p in range(64):
            if bpp == 4:
                packed = data[tile * tile_bytes + p // 2]
                index = ((packed >> (4 * (p & 1))) & 15) + palette_index * 16
            else:
                index = data[tile * tile_bytes + p]
            pos = ((tile_y + p // 8) * width + tile_x + p % 8) * 3
            pixels[pos:pos + 3] = colors[index]
    return width, height, bytes(pixels)


def _signed(value: int, bits: int) -> int:
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


def _visible_y_ranges(y: int, height: int) -> list[list[int]]:
    """The DS wraps OBJ Y modulo 256, not signed 8-bit at 128."""
    ranges: list[list[int]] = []
    for line in range(192):
        if ((line - y) & 255) < height:
            if ranges and ranges[-1][1] == line:
                ranges[-1][1] += 1
            else:
                ranges.append([line, line + 1])
    return ranges


def decode_oam(data: bytes) -> list[dict]:
    if len(data) != 1024:
        raise ValueError("one engine OAM must contain exactly 1024 bytes")
    entries = []
    for index in range(128):
        a0, a1, a2, a3 = struct.unpack_from("<4H", data, index * 8)
        affine = bool(a0 & 0x100)
        disabled = not affine and bool(a0 & 0x200)
        double_size = affine and bool(a0 & 0x200)
        shape, size = a0 >> 14, a1 >> 14
        mode = (a0 >> 10) & 3
        x_raw, y_raw = a1 & 511, a0 & 255
        width, height = SPRITE_SIZES[shape][size] if shape < 3 else (None, None)
        bound_width = width * (2 if double_size else 1) if width else None
        bound_height = height * (2 if double_size else 1) if height else None
        matrix_index = (a1 >> 9) & 31 if affine else None
        matrix = None
        if affine:
            base = matrix_index * 32 + 6
            coefficients = [struct.unpack_from("<h", data, base + i * 8)[0] for i in range(4)]
            matrix = {"index": matrix_index, "signed_8_8": coefficients,
                      "float": [value / 256 for value in coefficients],
                      "order": ["pa", "pb", "pc", "pd"]}
        entries.append({
            "index": index, "raw": {"attr0": a0, "attr1": a1, "attr2": a2,
                                       "parameter_word": a3},
            "x": _signed(x_raw, 9), "x_raw": x_raw,
            # Keep y=128..191 positive: those are visible DS screen rows.
            "y": y_raw if y_raw < 192 else y_raw - 256, "y_raw": y_raw,
            "y_wrap_period": 256,
            "visible_y_ranges": _visible_y_ranges(y_raw, bound_height) if bound_height and not disabled else [],
            "disabled": disabled, "affine": affine, "double_size": double_size,
            "affine_matrix": matrix,
            "hflip": None if affine else bool(a1 & 0x1000),
            "vflip": None if affine else bool(a1 & 0x2000),
            "shape": ("square", "horizontal", "vertical", "reserved")[shape],
            "shape_code": shape, "size_code": size, "valid_shape": shape < 3,
            "width": width, "height": height,
            "bounding_width": bound_width, "bounding_height": bound_height,
            "mode": ("normal", "semi_transparent", "obj_window", "bitmap")[mode],
            "mode_code": mode, "mosaic": bool(a0 & 0x1000),
            "bpp": 16 if mode == 3 else (8 if a0 & 0x2000 else 4),
            "color_256_flag": bool(a0 & 0x2000),
            "tile_index": a2 & 1023, "priority": (a2 >> 10) & 3,
            "palette_bank": a2 >> 12 if mode != 3 else None,
            "bitmap_alpha_raw": a2 >> 12 if mode == 3 else None,
            "bitmap_alpha_zero": mode == 3 and (a2 >> 12) == 0,
        })
    return entries


def decode_state(words: list[int]) -> dict:
    if len(words) != 16:
        raise ValueError("GPU state must contain 16 words")
    return {
        "frame_number": words[0], "vcount": words[1],
        "dispcnt": {"A": words[2], "B": words[3]}, "powcnt1": words[4],
        "vram_banks": [
            {"bank": bank, "size_bytes": size, "vramcnt": words[5 + i],
             "enabled": bool(words[5 + i] & 0x80),
             "mst_raw": words[5 + i] & 7, "offset_raw": (words[5 + i] >> 3) & 3}
            for i, (bank, size) in enumerate(zip(VRAM_BANKS, VRAM_SIZES))
        ],
        "reserved": words[14:16],
    }


def tilemap_source_length(map_data: bytes, bpp: int, map_width: int, map_height: int) -> int:
    if type(bpp) is not int or bpp not in (4, 8):
        raise ValueError("bpp must be 4 or 8")
    if type(map_width) is not int or map_width not in (32, 64):
        raise ValueError("map_width must be 32 or 64 tiles")
    if type(map_height) is not int or map_height not in (32, 64):
        raise ValueError("map_height must be 32 or 64 tiles")
    if len(map_data) != map_width * map_height * 2:
        raise ValueError("text map byte length does not match dimensions")
    return (max(entry[0] & 1023 for entry in struct.iter_unpack("<H", map_data)) + 1) * 8 * bpp


def tilemap_pixels(map_data: bytes, tiles: bytes, palette: bytes, bpp: int,
                   map_width: int, map_height: int) -> tuple[int, int, bytes]:
    """DS text BG: row-major 32x32 screenblocks, 10-bit tile IDs, H/V flips.

    This reproduces DrawBG_Text's screenblock addressing but intentionally does
    not apply scrolling, mosaic, windows, priorities or final screen blending.
    """
    required = tilemap_source_length(map_data, bpp, map_width, map_height)
    if len(tiles) != required:
        raise ValueError("tile source length does not match the largest map tile index")
    colors = [bytes(entry["rgb"]) + b"\xff" for entry in decode_palette(palette)]
    width, height = map_width * 8, map_height * 8
    pixels = bytearray(width * height * 4)
    tile_bytes = 8 * bpp
    blocks_per_row = map_width // 32
    for ty in range(map_height):
        for tx in range(map_width):
            block = (ty // 32) * blocks_per_row + tx // 32
            entry_offset = (block * 1024 + (ty % 32) * 32 + tx % 32) * 2
            entry = struct.unpack_from("<H", map_data, entry_offset)[0]
            tile_start = (entry & 1023) * tile_bytes
            palette_base = (entry >> 12) * 16 if bpp == 4 else 0
            for y in range(8):
                sy = 7 - y if entry & 0x800 else y
                for x in range(8):
                    sx = 7 - x if entry & 0x400 else x
                    if bpp == 4:
                        packed = tiles[tile_start + sy * 4 + sx // 2]
                        color = (packed >> (4 * (sx & 1))) & 15
                    else:
                        color = tiles[tile_start + sy * 8 + sx]
                    if color:
                        pos = (((ty * 8 + y) * width) + tx * 8 + x) * 4
                        pixels[pos:pos + 4] = colors[palette_base + color]
    return width, height, bytes(pixels)


def sprite_source_layout(sprite: dict, dispcnt: int) -> dict:
    """The explicit source offset replaces only the base address, not DS stride.

    DrawSprite_Normal/Rotscale use 32 * 32-byte units (1024 bytes) per
    eight-pixel tile row in 2D mode, also for 8bpp sprites. In 1D mode each
    sprite tile row is tightly packed. DISPCNT boundary bits affect the base
    address only, already resolved by the caller's physical source offset.
    """
    if sprite["disabled"]:
        raise ValueError("OAM entry is disabled; no sprite image is rendered")
    if not sprite["valid_shape"]:
        raise ValueError("reserved OAM shape cannot be rendered as a valid sprite")
    if sprite["mode"] == "bitmap":
        raise ValueError("bitmap OBJ mode is not supported by this tiled sprite inspector")
    bpp = sprite["bpp"]
    if bpp == 8 and (dispcnt & (1 << 31)) and sprite["mode"] != "obj_window":
        raise ValueError("extended OBJ palettes are not supported; standard palette inspection would be misleading")
    width, height = sprite["width"], sprite["height"]
    tile_bytes = 8 * bpp
    layout = "1d" if dispcnt & 0x10 else "2d"
    row_stride = (width // 8) * tile_bytes if layout == "1d" else 1024
    length = (height // 8 - 1) * row_stride + (width // 8) * tile_bytes
    return {"layout": layout, "row_stride_bytes": row_stride, "length": length,
            "tile_bytes": tile_bytes}


def sprite_pixels(data: bytes, palette: bytes, sprite: dict,
                  dispcnt: int) -> tuple[int, int, bytes]:
    layout = sprite_source_layout(sprite, dispcnt)
    if len(data) != layout["length"]:
        raise ValueError("sprite source length does not match DS tiled layout")
    colors = [bytes(entry["rgb"]) + b"\xff" for entry in decode_palette(palette)]
    width, height = sprite["width"], sprite["height"]
    out_width, out_height = sprite["bounding_width"], sprite["bounding_height"]
    bpp = sprite["bpp"]
    palette_base = sprite["palette_bank"] * 16 if bpp == 4 else 0
    pixels = bytearray(out_width * out_height * 4)
    if sprite["affine"]:
        pa, pb, pc, pd = sprite["affine_matrix"]["signed_8_8"]
    for y in range(out_height):
        for x in range(out_width):
            if sprite["affine"]:
                dx, dy = x - out_width // 2, y - out_height // 2
                rot_x = dx * pa + dy * pb + (width << 7)
                rot_y = dx * pc + dy * pd + (height << 7)
                if not (0 <= rot_x < width << 8 and 0 <= rot_y < height << 8):
                    continue
                sx, sy = rot_x >> 8, rot_y >> 8
            else:
                sx = width - 1 - x if sprite["hflip"] else x
                sy = height - 1 - y if sprite["vflip"] else y
            source = ((sy // 8) * layout["row_stride_bytes"]
                      + (sx // 8) * layout["tile_bytes"] + (sy % 8) * bpp)
            if bpp == 4:
                color = (data[source + (sx % 8) // 2] >> (4 * (sx & 1))) & 15
            else:
                color = data[source + sx % 8]
            if color:
                pos = (y * out_width + x) * 4
                pixels[pos:pos + 4] = (b"\xff\xff\xff\xff" if sprite["mode"] == "obj_window"
                                       else colors[palette_base + color])
    return out_width, out_height, bytes(pixels)
