"""GPU decoders and MCP boundaries; fake physical storage, no ROM required."""

import asyncio
import base64
import hashlib
import io
import struct
import threading
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult
from PIL import Image

from melonds_mcp.graphics import (
    VRAM_SIZES, decode_oam, decode_palette, decode_state, palette_pixels,
    rgb555, tile_pixels, vram_range,
    tilemap_pixels, tilemap_source_length, sprite_pixels, sprite_source_layout,
)
from melonds_mcp.tool_boundary import ToolBoundary
from melonds_mcp import tools_graphics


def palette_bytes():
    return struct.pack("<256H", *range(256))


@pytest.mark.parametrize("value,expected", [
    (0, (0, 0, 0)), (0x001F, (255, 0, 0)), (0x03E0, (0, 255, 0)),
    (0x7C00, (0, 0, 255)), (0x7FFF, (255, 255, 255)),
    (0xFFFF, (255, 255, 255)), (0x4210, (132, 132, 132)),
])
def test_rgb555_red_is_low_five_bits(value, expected):
    assert rgb555(value) == expected


def test_palette_entries_and_row_major_swatch():
    data = palette_bytes()
    entries = decode_palette(data)
    assert len(entries) == 256
    assert entries[31] == {"index": 31, "raw": 31, "rgb": [255, 0, 0],
                           "hex": "#ff0000", "bit15": False}
    width, height, pixels = palette_pixels(data, cell_size=2)
    assert (width, height) == (32, 32)
    assert pixels[(2 * width + 0) * 3:(2 * width + 0) * 3 + 3] == bytes(rgb555(16))
    assert pixels[(1 * width + 3) * 3:(1 * width + 3) * 3 + 3] == bytes(rgb555(1))


def test_4bpp_low_nibble_first_and_palette_bank():
    data = bytes([0x21, 0x43, 0x65, 0x87]) * 8
    width, height, pixels = tile_pixels(data, palette_bytes(), 4, 3, 1, 1)
    assert (width, height) == (8, 8)
    for x in range(8):
        assert pixels[x * 3:x * 3 + 3] == bytes(rgb555(48 + x + 1))


def test_8bpp_tile_sheet_positions_and_black_padding():
    data = bytes(range(64)) + bytes([200]) * 64 + bytes([250]) * 64
    width, height, pixels = tile_pixels(data, palette_bytes(), 8, 0, 3, 2)
    assert (width, height) == (16, 16)
    def pixel(x, y):
        pos = (y * width + x) * 3
        return pixels[pos:pos + 3]
    assert pixel(0, 0) == bytes(rgb555(0))
    assert pixel(7, 7) == bytes(rgb555(63))
    assert pixel(8, 0) == bytes(rgb555(200))
    assert pixel(0, 8) == bytes(rgb555(250))
    assert pixel(8, 8) == b"\0\0\0"


@pytest.mark.parametrize("bpp,palette_index,count,columns,length", [
    (2, 0, 1, 1, 32), (8, 1, 1, 1, 64), (4, 16, 1, 1, 32),
    (4, 0, 0, 1, 0), (4, 0, 257, 1, 8224), (4, 0, 1, 0, 32),
    (4, 0, 1, 33, 32), (4, 0, 1, 1, 31), (4, 0, True, 1, 32),
])
def test_tile_decoder_rejects_invalid_input(bpp, palette_index, count, columns, length):
    with pytest.raises(ValueError):
        tile_pixels(bytes(length), palette_bytes(), bpp, palette_index, count, columns)


def test_physical_bank_bounds_have_no_mirroring():
    for i, bank in enumerate("ABCDEFGHI"):
        assert vram_range(bank, VRAM_SIZES[i] - 1, 1) == i
        with pytest.raises(ValueError):
            vram_range(bank, VRAM_SIZES[i] - 1, 2)
    for bank, offset, length in (("J", 0, 1), ("A", -1, 1), ("A", 0, 0), ("A", True, 1)):
        with pytest.raises(ValueError):
            vram_range(bank, offset, length)


def test_oam_signed_x_but_visible_y_128_191_is_positive():
    data = bytearray(1024)
    # x=-1, y=150, horizontal 64x32, hflip/vflip, 8bpp, semitransparent.
    struct.pack_into("<4H", data, 0, 150 | 0x400 | 0x2000 | 0x4000,
                     511 | 0x3000 | 0xC000, 7 | (2 << 10) | (4 << 12), 0)
    # Non-affine attr0 bit9 disables the sprite (not double-size).
    struct.pack_into("<H", data, 8, 200 | 0x200)
    sprites = decode_oam(bytes(data))
    assert len(sprites) == 128
    first = sprites[0]
    assert (first["x"], first["y"], first["y_raw"]) == (-1, 150, 150)
    assert (first["width"], first["height"]) == (64, 32)
    assert first["mode"] == "semi_transparent"
    assert first["bpp"] == 8
    assert first["priority"] == 2
    assert first["palette_bank"] == 4
    assert first["hflip"] is first["vflip"] is True
    assert first["visible_y_ranges"] == [[150, 182]]
    second = sprites[1]
    assert second["y"] == -56
    assert second["disabled"] is True
    assert second["double_size"] is False
    assert second["visible_y_ranges"] == []


def test_oam_affine_double_size_shared_matrix_and_y_wrap():
    data = bytearray(1024)
    # Square 64x64 becomes 128x128 bounding box, matrix #1.
    struct.pack_into("<4H", data, 0, 150 | 0x300, 0xC000 | (1 << 9), 0, 0)
    for i, coefficient in enumerate((256, -128, 64, -256)):
        struct.pack_into("<h", data, 1 * 32 + 6 + i * 8, coefficient)
    first = decode_oam(bytes(data))[0]
    assert first["disabled"] is False
    assert first["affine"] is first["double_size"] is True
    assert first["bounding_width"] == first["bounding_height"] == 128
    assert first["visible_y_ranges"] == [[0, 22], [150, 192]]
    assert first["hflip"] is first["vflip"] is None
    assert first["affine_matrix"]["signed_8_8"] == [256, -128, 64, -256]
    assert first["affine_matrix"]["float"] == [1, -0.5, 0.25, -1]


def test_oam_bitmap_and_reserved_shape_are_not_falsely_composed():
    data = bytearray(1024)
    struct.pack_into("<4H", data, 0, 0x0C00 | 0xC000, 0, 0, 0)
    first = decode_oam(bytes(data))[0]
    assert first["mode"] == "bitmap"
    assert first["bpp"] == 16
    assert first["bitmap_alpha_zero"] is True
    assert first["palette_bank"] is None
    assert first["valid_shape"] is False
    assert first["width"] is first["height"] is None


@pytest.mark.parametrize("fn,data", [(decode_palette, bytes(511)), (decode_oam, bytes(1023)),
                                    (decode_state, [0] * 15)])
def test_decoders_reject_truncated_native_data(fn, data):
    with pytest.raises(ValueError):
        fn(data)


class FakeGPU:
    def __init__(self):
        self.reads = []
        self.state_reads = 0
        self.short_read = False
        self.dispcnt = [0x10000, 0x20000]
        self.regions = {(0, i): bytes([i]) * size for i, size in enumerate(VRAM_SIZES)}
        self.regions.update({(1, i): palette_bytes() * 2 for i in range(2)})
        self.regions.update({(2, i): bytes(1024) for i in range(2)})

    def melonds_gpu_state(self, words, capacity):
        assert capacity == 16
        self.state_reads += 1
        for i, word in enumerate([123, 45, *self.dispcnt, 0x820F] + list(range(0x80, 0x89)) + [0, 0]):
            words[i] = word
        return 16

    def melonds_gpu_read(self, region, bank, offset, dest, length):
        self.reads.append((region, bank, offset, length))
        source = self.regions[region, bank][offset:offset + length]
        for i, value in enumerate(source):
            dest[i] = value
        return len(source) - 1 if self.short_read else len(source)


@pytest.fixture
def server_and_native():
    native = FakeGPU()
    emu = SimpleNamespace(lib=SimpleNamespace(lib=native), lock=threading.RLock(), ensure_init=lambda: None)
    server = FastMCP("graphics-test")
    tools_graphics.register(ToolBoundary(server, emu), emu)
    return server, native


def invoke(server, name, args=None):
    return asyncio.run(server._tool_manager.get_tool(name).run(args or {}))


def test_seven_tools_registered_with_explicit_constraints(server_and_native):
    server, _ = server_and_native
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert set(tools) == {"gpu_state", "gpu_read_vram", "gpu_palette", "gpu_tiles", "gpu_oam",
                          "gpu_tilemap", "gpu_sprite"}
    assert tools["gpu_palette"].inputSchema["properties"]["kind"]["enum"] == ["bg", "obj"]
    assert tools["gpu_tiles"].inputSchema["properties"]["tile_count"]["maximum"] == 256
    assert tools["gpu_read_vram"].inputSchema["properties"]["length"]["maximum"] == 4096


def test_raw_read_is_physical_hashed_and_stamped(server_and_native):
    server, native = server_and_native
    result = invoke(server, "gpu_read_vram", {"bank": "I", "offset": 10, "length": 64})
    assert result["hex"] == "08" * 64
    assert result["sha256"] == hashlib.sha256(bytes([8]) * 64).hexdigest()
    assert (result["frame_number"], result["vcount"]) == (123, 45)
    assert native.reads == [(0, 8, 10, 64)]


@pytest.mark.parametrize("name,args", [
    ("gpu_read_vram", {"bank": "Z"}),
    ("gpu_read_vram", {"bank": "F", "offset": 16383, "length": 2}),
    ("gpu_read_vram", {"bank": "A", "length": 4097}),
    ("gpu_read_vram", {"bank": "A", "offset": True}),
    ("gpu_palette", {"engine": "C"}),
    ("gpu_palette", {"kind": "rw"}),
    ("gpu_tiles", {"bank": "A", "bpp": 2}),
    ("gpu_tiles", {"bank": "A", "bpp": 8, "palette_index": 1}),
    ("gpu_tiles", {"bank": "A", "tile_count": 257}),
    ("gpu_tiles", {"bank": "I", "offset": 16000, "tile_count": 16}),
    ("gpu_tiles", {"bank": "A", "columns": 0}),
    ("gpu_oam", {"engine": "A", "unexpected": 1}),
])
def test_bad_tool_arguments_do_not_touch_gpu(server_and_native, name, args):
    server, native = server_and_native
    with pytest.raises(ToolError):
        invoke(server, name, args)
    assert native.reads == []
    assert native.state_reads == 0


@pytest.mark.parametrize("name,args,size,reads", [
    ("gpu_palette", {"engine": "B", "kind": "obj"}, (192, 192), [(1, 1, 512, 512)]),
    ("gpu_tiles", {"bank": "B", "bpp": 8, "tile_count": 2, "columns": 2},
     (16, 8), [(0, 1, 0, 128), (1, 0, 0, 512)]),
])
def test_graphics_images_are_protocol_content_with_metadata(server_and_native, name, args, size, reads):
    server, native = server_and_native
    result = asyncio.run(server.call_tool(name, args))
    assert isinstance(result, CallToolResult)
    assert [c.type for c in result.content] == ["text", "image"]
    png = base64.b64decode(result.content[1].data)
    assert result.content[1].mimeType == "image/png"
    assert Image.open(io.BytesIO(png)).size == size
    assert result.structuredContent["png_sha256"] == hashlib.sha256(png).hexdigest()
    assert result.structuredContent["frame_number"] == 123
    assert "data_base64" not in result.structuredContent
    assert native.reads == reads


def test_oam_tool_returns_128_structured_entries_without_advancing(server_and_native):
    server, native = server_and_native
    result = invoke(server, "gpu_oam", {"engine": "B"})
    assert result["entry_count"] == len(result["entries"]) == 128
    assert result["composed"] is False
    assert result["frame_number"] == 123
    assert native.reads == [(2, 1, 0, 1024)]


def test_short_native_read_is_error_not_padded_image(server_and_native):
    server, native = server_and_native
    native.short_read = True
    with pytest.raises(ToolError, match="native GPU read failed"):
        invoke(server, "gpu_palette")


def rgba_at(pixels, width, x, y):
    start = (y * width + x) * 4
    return pixels[start:start + 4]


def colored_rgba(index):
    return bytes(rgb555(index)) + b"\xff"


@pytest.mark.parametrize("map_width,map_height", [(32, 32), (64, 32), (32, 64), (64, 64)])
def test_text_map_uses_row_major_screenblocks_not_linear_rows(map_width, map_height):
    count = (map_width // 32) * (map_height // 32)
    map_data = b"".join(struct.pack("<H", i) * 1024 for i in range(count))
    tiles = b"".join(bytes([i + 1]) * 64 for i in range(count))
    width, height, pixels = tilemap_pixels(map_data, tiles, palette_bytes(), 8, map_width, map_height)
    assert (width, height) == (map_width * 8, map_height * 8)
    for y in range(0, height, 256):
        for x in range(0, width, 256):
            block = (y // 256) * (map_width // 32) + x // 256
            assert rgba_at(pixels, width, x, y) == colored_rgba(block + 1)
            assert rgba_at(pixels, width, x + 255, y + 255) == colored_rgba(block + 1)


@pytest.mark.parametrize("flags", [0, 0x400, 0x800, 0xC00])
def test_text_map_hv_flips_and_8bpp_palette_bits_ignored(flags):
    map_data = struct.pack("<H", flags | 0xF000) * 1024
    tiles = bytes(range(64))
    width, _, pixels = tilemap_pixels(map_data, tiles, palette_bytes(), 8, 32, 32)
    for y in range(8):
        for x in range(8):
            sx = 7 - x if flags & 0x400 else x
            sy = 7 - y if flags & 0x800 else y
            index = sy * 8 + sx
            assert rgba_at(pixels, width, x, y) == (colored_rgba(index) if index else bytes(4))


def test_text_map_4bpp_palette_bank_transparency_and_10bit_index():
    # Entry references tile 1023 and standard palette bank 3, not tile 0x33ff.
    map_data = struct.pack("<H", 0x33FF) * 1024
    tiles = bytes(1023 * 32) + b"\x10" * 32
    assert tilemap_source_length(map_data, 4, 32, 32) == 32768
    width, _, pixels = tilemap_pixels(map_data, tiles, palette_bytes(), 4, 32, 32)
    assert rgba_at(pixels, width, 0, 0) == bytes(4)
    assert rgba_at(pixels, width, 1, 0) == colored_rgba(49)


@pytest.mark.parametrize("data,bpp,width,height", [(bytes(2048), 2, 32, 32),
    (bytes(2048), 4, 16, 32), (bytes(2048), 4, 32, 128), (bytes(2047), 4, 32, 32)])
def test_text_map_limits_and_truncation(data, bpp, width, height):
    with pytest.raises(ValueError):
        tilemap_source_length(data, bpp, width, height)


def sprite_entry(attr0=0, attr1=0, attr2=0, coefficients=(256, 0, 0, 256)):
    data = bytearray(1024)
    struct.pack_into("<3H", data, 0, attr0, attr1, attr2)
    for i, value in enumerate(coefficients):
        struct.pack_into("<h", data, 6 + i * 8, value)
    return decode_oam(bytes(data))[0]


@pytest.mark.parametrize("bpp", [4, 8])
@pytest.mark.parametrize("dispcnt", [0, 0x10])
def test_sprite_1d_2d_stride_is_ds_32byte_units_for_both_bpp(bpp, dispcnt):
    sprite = sprite_entry(0x2000 if bpp == 8 else 0, 0x4000)  # 16x16
    layout = sprite_source_layout(sprite, dispcnt)
    assert layout["row_stride_bytes"] == (2 * 8 * bpp if dispcnt else 1024)
    source = bytearray(layout["length"])
    for ty in range(2):
        for tx in range(2):
            color = ty * 2 + tx + 1
            start = ty * layout["row_stride_bytes"] + tx * layout["tile_bytes"]
            source[start:start + layout["tile_bytes"]] = bytes([color if bpp == 8 else color * 17]) * layout["tile_bytes"]
    width, height, pixels = sprite_pixels(bytes(source), palette_bytes(), sprite, dispcnt)
    assert (width, height) == (16, 16)
    assert rgba_at(pixels, width, 0, 0) == colored_rgba(1)
    assert rgba_at(pixels, width, 15, 0) == colored_rgba(2)
    assert rgba_at(pixels, width, 0, 15) == colored_rgba(3)
    assert rgba_at(pixels, width, 15, 15) == colored_rgba(4)


@pytest.mark.parametrize("flags", [0, 0x1000, 0x2000, 0x3000])
def test_sprite_normal_flip_and_transparency(flags):
    sprite = sprite_entry(0x2000, flags)
    width, _, pixels = sprite_pixels(bytes(range(64)), palette_bytes(), sprite, 0x10)
    for y in range(8):
        for x in range(8):
            sx = 7 - x if flags & 0x1000 else x
            sy = 7 - y if flags & 0x2000 else y
            color = sy * 8 + sx
            assert rgba_at(pixels, width, x, y) == (colored_rgba(color) if color else bytes(4))


def test_sprite_4bpp_palette_bank_and_obj_window_mask():
    sprite = sprite_entry(0, 0, 5 << 12)
    width, _, pixels = sprite_pixels(b"\x10" * 32, palette_bytes(), sprite, 0x10)
    assert rgba_at(pixels, width, 0, 0) == bytes(4)
    assert rgba_at(pixels, width, 1, 0) == colored_rgba(81)
    window = sprite_entry(0x800, 0, 5 << 12)
    width, _, pixels = sprite_pixels(b"\x10" * 32, palette_bytes(), window, 0x10)
    assert rgba_at(pixels, width, 0, 0) == bytes(4)
    assert rgba_at(pixels, width, 1, 0) == b"\xff" * 4


def test_sprite_affine_identity_double_size_centered_and_transparent_border():
    sprite = sprite_entry(0x300)
    width, height, pixels = sprite_pixels(b"\x11" * 32, palette_bytes(), sprite, 0x10)
    assert (width, height) == (16, 16)
    for y in range(height):
        for x in range(width):
            expected = colored_rgba(1) if 4 <= x < 12 and 4 <= y < 12 else bytes(4)
            assert rgba_at(pixels, width, x, y) == expected


def test_sprite_affine_signed_inverse_sampling_matches_core_center_formula():
    sprite = sprite_entry(0x2100, coefficients=(0, 256, -256, 0))
    width, _, pixels = sprite_pixels(bytes(range(64)), palette_bytes(), sprite, 0x10)
    # rotX=y, rotY=8-x: x=0 is outside. This is inverse sampling, not a
    # generic image rotation around (width-1)/2, which would shift one pixel.
    assert rgba_at(pixels, width, 0, 2) == bytes(4)
    assert rgba_at(pixels, width, 1, 2) == colored_rgba(7 * 8 + 2)
    assert rgba_at(pixels, width, 7, 3) == colored_rgba(1 * 8 + 3)


@pytest.mark.parametrize("attr0,dispcnt,match", [(0x200, 0, "disabled"),
    (0xC000, 0, "reserved"), (0xC00, 0, "bitmap"),
    (0x2000, 1 << 31, "extended")])
def test_sprite_unsupported_modes_fail_explicitly(attr0, dispcnt, match):
    with pytest.raises(ValueError, match=match):
        sprite_source_layout(sprite_entry(attr0), dispcnt)


def test_new_tools_emit_rgba_without_inferred_mapping(server_and_native):
    server, native = server_and_native
    # All entries tile0; explicit tile bank B contains palette index1 bytes.
    tilemap = invoke(server, "gpu_tilemap", {"map_bank": "A", "map_offset": 0,
                      "tile_bank": "B", "tile_offset": 0, "bpp": 8})
    assert tilemap.structuredContent["view"] == "raw_text_bg_tilemap"
    assert tilemap.structuredContent["pixel_format"] == "RGBA8"
    assert tilemap.structuredContent["size"] == {"width": 256, "height": 256}
    assert tilemap.structuredContent["tile_length"] == 64
    assert native.reads == [(0, 0, 0, 2048), (0, 1, 0, 64), (1, 0, 0, 512)]
    native.reads.clear()
    sprite = invoke(server, "gpu_sprite", {"engine": "A", "index": 0, "bank": "B", "offset": 64})
    assert sprite.structuredContent["view"] == "single_sprite"
    assert sprite.structuredContent["pixel_format"] == "RGBA8"
    assert sprite.structuredContent["offset"] == 64
    assert sprite.structuredContent["composed"] is False
    assert sprite.structuredContent["frame_number"] == 123
    assert native.reads == [(2, 0, 0, 1024), (0, 1, 64, 32), (1, 0, 512, 512)]


@pytest.mark.parametrize("name,args", [
    ("gpu_tilemap", {"map_bank": "I", "map_offset": 16000, "tile_bank": "A", "tile_offset": 0}),
    ("gpu_tilemap", {"map_bank": "A", "map_offset": 0, "tile_bank": "I", "tile_offset": 16380}),
    ("gpu_tilemap", {"map_bank": "A", "map_offset": 0, "tile_bank": "B", "tile_offset": 0, "map_width": 128}),
    ("gpu_sprite", {"engine": "A", "index": 128, "bank": "A"}),
    ("gpu_sprite", {"engine": "A", "index": 0, "bank": "I", "offset": 16384}),
])
def test_new_tool_front_boundaries_fail_before_gpu_reads(server_and_native, name, args):
    server, native = server_and_native
    with pytest.raises(ToolError):
        invoke(server, name, args)
    assert native.reads == []


def test_map_referenced_tiles_cannot_cross_physical_bank(server_and_native):
    server, native = server_and_native
    native.regions[0, 0] = struct.pack("<H", 1023) * (VRAM_SIZES[0] // 2)
    with pytest.raises(ToolError, match="physical VRAM bank|length must"):
        invoke(server, "gpu_tilemap", {"map_bank": "A", "map_offset": 0,
               "tile_bank": "I", "tile_offset": 0, "bpp": 8})
    # The map must be read to discover its references, but no tile/palette read.
    assert native.reads == [(0, 0, 0, 2048)]


def test_sprite_source_stride_cannot_cross_physical_bank(server_and_native):
    server, native = server_and_native
    with pytest.raises(ToolError, match="physical VRAM bank"):
        invoke(server, "gpu_sprite", {"engine": "A", "index": 0, "bank": "I", "offset": 16380})
    assert native.reads == [(2, 0, 0, 1024)]


def test_tilemap_extended_palette_is_not_misrepresented_as_standard(server_and_native):
    server, native = server_and_native
    native.dispcnt[0] |= 1 << 30
    with pytest.raises(ToolError, match="extended BG palettes"):
        invoke(server, "gpu_tilemap", {"map_bank": "A", "map_offset": 0,
               "tile_bank": "B", "tile_offset": 0, "bpp": 8})
    assert native.reads == []


@pytest.mark.parametrize("attr0,expected", [(0x200, "disabled"), (0xC00, "bitmap"),
                                         (0xC000, "reserved")])
def test_sprite_tool_refuses_unsupported_oam_before_source_read(server_and_native, attr0, expected):
    server, native = server_and_native
    native.regions[2, 0] = struct.pack("<H", attr0) + bytes(1022)
    with pytest.raises(ToolError, match=expected):
        invoke(server, "gpu_sprite", {"engine": "A", "index": 0, "bank": "B"})
    assert native.reads == [(2, 0, 0, 1024)]


def test_sprite_maximum_affine_size_is_bounded_to_128_square():
    sprite = sprite_entry(0x2300, 0xC000)
    layout = sprite_source_layout(sprite, 0x10)
    assert layout["length"] == 4096
    width, height, pixels = sprite_pixels(bytes([1]) * layout["length"], palette_bytes(), sprite, 0x10)
    assert (width, height, len(pixels)) == (128, 128, 128 * 128 * 4)


def test_sprite_truncated_source_is_error():
    with pytest.raises(ValueError, match="source length"):
        sprite_pixels(bytes(31), palette_bytes(), sprite_entry(), 0x10)


def test_tilemap_truncated_referenced_tiles_is_error():
    with pytest.raises(ValueError, match="tile source length"):
        tilemap_pixels(bytes(2048), bytes(31), palette_bytes(), 4, 32, 32)
