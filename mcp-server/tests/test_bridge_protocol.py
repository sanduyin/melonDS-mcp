from __future__ import annotations

import struct

import pytest

from melonds_mcp.bridge_protocol import (
    HEADER_SIZE,
    BridgeFrame,
    FrameDecoder,
    MessageType,
    encode_frame,
)
from melonds_mcp.errors import BridgeProtocolError, ValidationError


def test_header_has_stable_golden_little_endian_layout() -> None:
    encoded = encode_frame(
        MessageType.REQUEST,
        0x0102030405060708,
        {"op": "status"},
        binary=b"\xaa\xbb",
    )
    expected_header = bytes.fromhex(
        "4d445342"  # MDSB
        "0100"  # protocol version 1
        "0100"  # request
        "00000000"  # flags
        "0807060504030201"  # request_id
        "0f000000"  # len({\"op\":\"status\"})
        "02000000"  # binary length
        "00000000"  # reserved
    )
    assert HEADER_SIZE == 32
    assert encoded[:HEADER_SIZE] == expected_header
    assert encoded[HEADER_SIZE:] == b'{"op":"status"}\xaa\xbb'


def test_incremental_decoder_handles_fragmentation_and_multiple_frames() -> None:
    first = encode_frame(MessageType.REQUEST, 7, {"op": "memory.read"})
    second = encode_frame(
        MessageType.RESPONSE,
        7,
        {"ok": True, "binary_format": "bgra8888"},
        binary=b"pixels",
    )
    decoder = FrameDecoder()
    frames: list[BridgeFrame] = []
    stream = first + second
    for offset in range(0, len(stream), 3):
        frames.extend(decoder.feed(stream[offset : offset + 3]))

    assert frames == [
        BridgeFrame(MessageType.REQUEST, 7, {"op": "memory.read"}),
        BridgeFrame(
            MessageType.RESPONSE,
            7,
            {"ok": True, "binary_format": "bgra8888"},
            b"pixels",
        ),
    ]
    assert decoder.buffered_bytes == 0


@pytest.mark.parametrize(
    ("field_offset", "replacement", "match"),
    [
        (0, b"NOPE", "magic"),
        (4, struct.pack("<H", 2), "version"),
        (6, struct.pack("<H", 99), "message type"),
        (8, struct.pack("<I", 1), "flags"),
        (28, struct.pack("<I", 1), "reserved"),
    ],
)
def test_decoder_rejects_invalid_header_fields(
    field_offset: int,
    replacement: bytes,
    match: str,
) -> None:
    frame = bytearray(encode_frame(MessageType.EVENT, 0, {"event": "stopped"}))
    frame[field_offset : field_offset + len(replacement)] = replacement
    with pytest.raises(BridgeProtocolError, match=match):
        FrameDecoder().feed(bytes(frame))


def test_decoder_rejects_oversized_payload_before_buffering_body() -> None:
    header = struct.pack(
        "<4sHHIQIII",
        b"MDSB",
        1,
        1,
        0,
        9,
        1025,
        0,
        0,
    )
    with pytest.raises(BridgeProtocolError, match="JSON payload length"):
        FrameDecoder(max_json_bytes=1024).feed(header)


def test_encoder_requires_object_and_bytes() -> None:
    with pytest.raises(ValidationError, match="JSON object"):
        encode_frame(MessageType.REQUEST, 1, ["not", "an", "object"])  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="binary must be bytes"):
        encode_frame(MessageType.REQUEST, 1, {}, binary=bytearray())  # type: ignore[arg-type]
