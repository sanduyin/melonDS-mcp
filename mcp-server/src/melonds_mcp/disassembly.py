"""ARM/Thumb disassembly helpers backed by Capstone."""

from __future__ import annotations

from typing import Literal

from .errors import MelonDSMCPError
from .values import hex_u32


def disassemble_arm(
    data: bytes,
    *,
    address: int,
    mode: Literal["arm", "thumb"],
    count: int,
    isa: Literal["armv4t", "armv5te"],
) -> list[dict[str, object]]:
    try:
        from capstone import (
            CS_ARCH_ARM,
            CS_MODE_ARM,
            CS_MODE_LITTLE_ENDIAN,
            CS_MODE_THUMB,
            Cs,
        )
    except ImportError as exc:  # pragma: no cover - dependency installation issue
        raise MelonDSMCPError(
            "disassembly requires Capstone; reinstall the melonds-mcp package"
        ) from exc

    engine_mode = CS_MODE_ARM if mode == "arm" else CS_MODE_THUMB
    engine = Cs(CS_ARCH_ARM, engine_mode | CS_MODE_LITTLE_ENDIAN)
    engine.detail = False
    if mode == "thumb":
        return _disassemble_thumb1(
            engine,
            data,
            address=address,
            count=count,
            isa=isa,
        )

    instructions: list[dict[str, object]] = []
    for instruction in engine.disasm(data, address, count=count):
        instructions.append(_format_instruction(instruction, isa_validated=False))
    return instructions


def _disassemble_thumb1(
    engine: object,
    data: bytes,
    *,
    address: int,
    count: int,
    isa: Literal["armv4t", "armv5te"],
) -> list[dict[str, object]]:
    """Decode Thumb-1 without allowing Capstone to consume Thumb-2 pairs."""

    instructions: list[dict[str, object]] = []
    offset = 0
    while offset + 2 <= len(data) and len(instructions) < count:
        first = int.from_bytes(data[offset : offset + 2], "little")
        width = 2
        if (first & 0xF800) == 0xF000 and offset + 4 <= len(data):
            second = int.from_bytes(data[offset + 2 : offset + 4], "little")
            is_bl = (second & 0xF800) == 0xF800
            is_blx = isa == "armv5te" and (second & 0xF800) == 0xE800
            if is_bl or is_blx:
                width = 4

        chunk = data[offset : offset + width]
        decoded = list(engine.disasm(chunk, address + offset, count=1))  # type: ignore[attr-defined]
        if (
            decoded
            and decoded[0].size == width
            and (width == 2 or decoded[0].mnemonic in {"bl", "blx"})
        ):
            instructions.append(
                _format_instruction(decoded[0], isa_validated=True)
            )
            offset += width
            continue

        # Undefined/reserved Thumb-1 halfwords remain visible and advance by one
        # architectural halfword instead of letting a newer Thumb-2 decoder shift
        # every following instruction boundary.
        raw = data[offset : offset + 2]
        instructions.append(
            {
                "address": hex_u32(address + offset),
                "size": 2,
                "bytes": raw.hex(),
                "mnemonic": ".hword",
                "operands": f"0x{first:04x}",
                "text": f".hword 0x{first:04x}",
                "isa_validated": False,
            }
        )
        offset += 2
    return instructions


def _format_instruction(
    instruction: object,
    *,
    isa_validated: bool,
) -> dict[str, object]:
    mnemonic = instruction.mnemonic  # type: ignore[attr-defined]
    operands = instruction.op_str  # type: ignore[attr-defined]
    return {
        "address": hex_u32(instruction.address),  # type: ignore[attr-defined]
        "size": instruction.size,  # type: ignore[attr-defined]
        "bytes": bytes(instruction.bytes).hex(),  # type: ignore[attr-defined]
        "mnemonic": mnemonic,
        "operands": operands,
        "text": mnemonic if not operands else f"{mnemonic} {operands}",
        "isa_validated": isa_validated,
    }
