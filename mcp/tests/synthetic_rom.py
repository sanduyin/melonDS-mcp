"""Original, freely redistributable ARM test program; no commercial ROM assets.

The header contains no Nintendo logo/BIOS data. melonDS direct-boots this tiny
homebrew cartridge using FreeBIOS and its generated firmware.
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from pathlib import Path
import struct

ROM_SIZE = 128 * 1024
CODE_BASES = (0x02000000, 0x02004000)
COUNTERS = (0x02001000, 0x02002000)
ROM_OFFSETS = (0x200, 0x400)
PROGRAM_SIZE = 0x200
REDIRECT_OFFSET = 0x100


def _crc16(data: bytes | bytearray, initial: int = 0xFFFF) -> int:
    result = initial
    for value in data:
        result ^= value
        for _ in range(8):
            result = (result >> 1) ^ (0xA001 if result & 1 else 0)
    return result


def build_rom() -> bytes:
    """Build two CPUs' increment/store loops and an alternate PC-write target."""
    rom = bytearray(ROM_SIZE)
    rom[:12] = b"MCP SMOKE\0\0\0"
    rom[0x0C:0x10] = b"####"  # Explicit homebrew game code.
    rom[0x10:0x12] = b"00"
    for cpu, (offset, base, counter) in enumerate(
        zip(ROM_OFFSETS, CODE_BASES, COUNTERS, strict=True)
    ):
        struct.pack_into("<4I", rom, 0x20 + cpu * 0x10,
                         offset, base, base, PROGRAM_SIZE)
        # ldr r0, [pc, #12]; mov r1, #0; add r1, r1, #1;
        # str r1, [r0]; b <add>; .word counter
        struct.pack_into("<6I", rom, offset,
                         0xE59F000C, 0xE3A01000, 0xE2811001,
                         0xE5801000, 0xEAFFFFFC, counter)
        # mov r2, #0x66; b . -- deliberately differs from the cached old stream.
        struct.pack_into("<2I", rom, offset + REDIRECT_OFFSET,
                         0xE3A02066, 0xEAFFFFFE)
    struct.pack_into("<2I", rom, 0x80, ROM_SIZE, 0x200)
    struct.pack_into("<H", rom, 0x15E, _crc16(rom[:0x15E]))
    return bytes(rom)


def write_rom(path: Path) -> Path:
    """Write only this generated fixture at the caller's chosen temporary path."""
    path.write_bytes(build_rom())
    return path
