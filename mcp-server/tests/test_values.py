from __future__ import annotations

import pytest

from melonds_mcp.errors import ValidationError
from melonds_mcp.values import parse_counter, parse_hex_bytes, parse_u32


@pytest.mark.parametrize(
    ("input_value", "expected"),
    [("0x0200_0000", 0x02000000), ("123", 123), (0xFFFFFFFF, 0xFFFFFFFF)],
)
def test_parse_u32(input_value: int | str, expected: int) -> None:
    assert parse_u32(input_value) == expected


@pytest.mark.parametrize("input_value", [True, -1, 0x1_0000_0000, "", "02000000"])
def test_parse_u32_rejects_ambiguous_or_out_of_range(input_value: object) -> None:
    with pytest.raises(ValidationError):
        parse_u32(input_value)  # type: ignore[arg-type]


def test_parse_hex_bytes_accepts_layout_whitespace() -> None:
    assert parse_hex_bytes("0x01 02_03\n04") == b"\x01\x02\x03\x04"


@pytest.mark.parametrize("input_value", [-1, True, "0x1", "one"])
def test_parse_counter_rejects_noncanonical_values(input_value: object) -> None:
    with pytest.raises(ValidationError):
        parse_counter(input_value, field="counter")  # type: ignore[arg-type]


@pytest.mark.parametrize(("input_value", "expected"), [("0", 0), ("01", 1), (9, 9)])
def test_parse_counter(input_value: int | str, expected: int) -> None:
    assert parse_counter(input_value, field="counter") == expected
