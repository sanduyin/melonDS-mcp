"""Validation and JSON formatting helpers shared by tools and backends."""

from __future__ import annotations

import re

from .errors import ValidationError

_HEX_RE = re.compile(r"^[0-9a-fA-F]*$")


def parse_u32(value: int | str, *, field: str = "value") -> int:
    """Parse a decimal/hex integer and constrain it to an unsigned ARM word."""
    if isinstance(value, bool):
        raise ValidationError(f"{field} must be an integer, not a boolean")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        text = value.strip().replace("_", "")
        if not text:
            raise ValidationError(f"{field} must not be empty")
        try:
            parsed = int(text, 0)
        except ValueError as exc:
            raise ValidationError(
                f"{field} must be decimal or 0x-prefixed hexadecimal"
            ) from exc
    else:
        raise ValidationError(f"{field} must be an integer or string")

    if not 0 <= parsed <= 0xFFFF_FFFF:
        raise ValidationError(f"{field} must be between 0 and 0xffffffff")
    return parsed


def parse_positive_int(
    value: int,
    *,
    field: str,
    maximum: int,
    minimum: int = 1,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} must be an integer")
    if not minimum <= value <= maximum:
        raise ValidationError(
            f"{field} must be between {minimum} and {maximum}"
        )
    return value


def parse_counter(value: int | str, *, field: str) -> int:
    """Parse a non-negative decimal counter without accepting bool/float/hex."""
    if isinstance(value, bool):
        raise ValidationError(f"{field} must be a non-negative decimal counter")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.isdecimal():
        if len(value) > 20:
            raise ValidationError(f"{field} must fit in an unsigned 64-bit counter")
        try:
            parsed = int(value, 10)
        except ValueError as exc:
            raise ValidationError(
                f"{field} must be a non-negative decimal counter"
            ) from exc
    else:
        raise ValidationError(f"{field} must be a non-negative decimal counter")
    if not 0 <= parsed <= 0xFFFF_FFFF_FFFF_FFFF:
        raise ValidationError(f"{field} must fit in an unsigned 64-bit counter")
    return parsed


def parse_hex_bytes(value: str, *, field: str = "data_hex") -> bytes:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a hexadecimal string")
    text = "".join(value.split())
    if text.lower().startswith("0x"):
        text = text[2:]
    text = text.replace("_", "")
    if len(text) % 2:
        raise ValidationError(f"{field} must contain an even number of hex digits")
    if not _HEX_RE.fullmatch(text):
        raise ValidationError(f"{field} contains a non-hexadecimal character")
    return bytes.fromhex(text)


def hex_u32(value: int) -> str:
    return f"0x{value & 0xFFFF_FFFF:08x}"
