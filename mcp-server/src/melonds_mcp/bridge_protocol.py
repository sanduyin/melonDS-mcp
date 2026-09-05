"""Versioned framing shared by the MCP facade and native melonDS bridge.

The transport is a byte stream (Windows named pipe or Unix domain socket). Each
frame has a fixed 32-byte little-endian header followed by one UTF-8 JSON object
and an optional opaque binary payload. Keeping bulk pixels/memory outside JSON
avoids base64 overhead while retaining an inspectable control plane.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import json
import struct
from typing import Any, Final, Mapping

from .errors import BridgeProtocolError, ValidationError


MAGIC: Final[bytes] = b"MDSB"
PROTOCOL_VERSION: Final[int] = 1
MAX_JSON_BYTES: Final[int] = 1 * 1024 * 1024
MAX_BINARY_BYTES: Final[int] = 64 * 1024 * 1024
HEADER: Final[struct.Struct] = struct.Struct("<4sHHIQIII")
HEADER_SIZE: Final[int] = HEADER.size


class MessageType(IntEnum):
    REQUEST = 1
    RESPONSE = 2
    EVENT = 3
    CANCEL = 4


@dataclass(frozen=True, slots=True)
class BridgeFrame:
    message_type: MessageType
    request_id: int
    document: dict[str, Any]
    binary: bytes = b""
    flags: int = 0


def encode_frame(
    message_type: MessageType | int,
    request_id: int,
    document: Mapping[str, Any],
    *,
    binary: bytes = b"",
    flags: int = 0,
) -> bytes:
    """Encode one bridge frame after validating all wire-level limits."""

    resolved_type = _message_type(message_type, error_type=ValidationError)
    _validate_u64(request_id, "request_id", error_type=ValidationError)
    if flags != 0:
        raise ValidationError("flags must be zero for bridge protocol version 1")
    if not isinstance(document, Mapping):
        raise ValidationError("document must be a JSON object")
    if not isinstance(binary, bytes):
        raise ValidationError("binary must be bytes")
    try:
        json_payload = json.dumps(
            dict(document),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"document is not JSON serializable: {exc}") from exc
    _validate_lengths(
        len(json_payload),
        len(binary),
        error_type=ValidationError,
    )
    header = HEADER.pack(
        MAGIC,
        PROTOCOL_VERSION,
        int(resolved_type),
        flags,
        request_id,
        len(json_payload),
        len(binary),
        0,
    )
    return header + json_payload + binary


class FrameDecoder:
    """Incrementally decode complete frames from an arbitrary byte stream."""

    def __init__(
        self,
        *,
        max_json_bytes: int = MAX_JSON_BYTES,
        max_binary_bytes: int = MAX_BINARY_BYTES,
    ) -> None:
        if not 1 <= max_json_bytes <= 0xFFFF_FFFF:
            raise ValidationError("max_json_bytes must be between 1 and 2^32-1")
        if not 0 <= max_binary_bytes <= 0xFFFF_FFFF:
            raise ValidationError("max_binary_bytes must be between 0 and 2^32-1")
        self.max_json_bytes = max_json_bytes
        self.max_binary_bytes = max_binary_bytes
        self._buffer = bytearray()

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, data: bytes) -> list[BridgeFrame]:
        if not isinstance(data, bytes):
            raise ValidationError("frame input must be bytes")
        self._buffer.extend(data)
        frames: list[BridgeFrame] = []
        while len(self._buffer) >= HEADER_SIZE:
            (
                magic,
                version,
                raw_type,
                flags,
                request_id,
                json_length,
                binary_length,
                reserved,
            ) = HEADER.unpack_from(self._buffer)
            if magic != MAGIC:
                raise BridgeProtocolError(
                    f"invalid bridge magic {magic!r}; expected {MAGIC!r}"
                )
            if version != PROTOCOL_VERSION:
                raise BridgeProtocolError(
                    f"unsupported bridge protocol version {version}"
                )
            message_type = _message_type(raw_type, error_type=BridgeProtocolError)
            if flags != 0:
                raise BridgeProtocolError(
                    f"unsupported bridge flags 0x{flags:08x} for protocol version 1"
                )
            if reserved != 0:
                raise BridgeProtocolError("reserved bridge header field must be zero")
            _validate_lengths(
                json_length,
                binary_length,
                max_json_bytes=self.max_json_bytes,
                max_binary_bytes=self.max_binary_bytes,
                error_type=BridgeProtocolError,
            )
            frame_size = HEADER_SIZE + json_length + binary_length
            if len(self._buffer) < frame_size:
                break
            json_start = HEADER_SIZE
            json_end = json_start + json_length
            binary_end = json_end + binary_length
            json_payload = bytes(self._buffer[json_start:json_end])
            binary = bytes(self._buffer[json_end:binary_end])
            del self._buffer[:frame_size]
            document = _decode_document(json_payload)
            frames.append(
                BridgeFrame(
                    message_type=message_type,
                    request_id=request_id,
                    document=document,
                    binary=binary,
                    flags=flags,
                )
            )
        return frames


def _decode_document(payload: bytes) -> dict[str, Any]:
    try:
        decoded = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BridgeProtocolError("bridge JSON payload is not valid UTF-8") from exc
    try:
        document = json.loads(decoded)
    except json.JSONDecodeError as exc:
        raise BridgeProtocolError(f"bridge JSON payload is invalid: {exc.msg}") from exc
    if not isinstance(document, dict):
        raise BridgeProtocolError("bridge JSON payload must be an object")
    return document


def _message_type(
    value: MessageType | int,
    *,
    error_type: type[BridgeProtocolError] | type[ValidationError],
) -> MessageType:
    if isinstance(value, bool):
        raise error_type("message_type must be one of 1, 2, 3, or 4")
    try:
        return MessageType(value)
    except (TypeError, ValueError) as exc:
        raise error_type(f"unsupported bridge message type {value!r}") from exc


def _validate_u64(
    value: int,
    field: str,
    *,
    error_type: type[BridgeProtocolError] | type[ValidationError],
) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFFFF_FFFF_FFFF_FFFF:
        raise error_type(f"{field} must be an unsigned 64-bit integer")


def _validate_lengths(
    json_length: int,
    binary_length: int,
    *,
    max_json_bytes: int = MAX_JSON_BYTES,
    max_binary_bytes: int = MAX_BINARY_BYTES,
    error_type: type[BridgeProtocolError] | type[ValidationError],
) -> None:
    if not 1 <= json_length <= max_json_bytes:
        raise error_type(
            f"JSON payload length {json_length} is outside 1..{max_json_bytes}"
        )
    if not 0 <= binary_length <= max_binary_bytes:
        raise error_type(
            f"binary payload length {binary_length} is outside 0..{max_binary_bytes}"
        )
