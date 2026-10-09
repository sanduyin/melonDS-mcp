"""Synchronous request client for the native melonDS local bridge."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import secrets
import socket
import threading
from typing import Any, Mapping, Protocol

from .bridge_protocol import (
    BridgeFrame,
    FrameDecoder,
    MessageType,
    encode_frame,
)
from .errors import (
    BridgeConnectionError,
    BridgeProtocolError,
    BridgeRemoteError,
    SessionError,
    ValidationError,
)


class ByteStream(Protocol):
    def sendall(self, data: bytes) -> None: ...

    def recv(self, size: int) -> bytes: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class BridgeResponse:
    request_id: int
    result: dict[str, Any]
    binary: bytes


@dataclass(frozen=True, slots=True)
class BridgeEvent:
    name: str
    sequence: int
    data: dict[str, Any]
    binary: bytes


class NativeBridgeClient:
    """Serialize one in-flight request while accepting ordered bridge events."""

    def __init__(self, stream: ByteStream, *, receive_chunk_bytes: int = 65_536) -> None:
        if not 1 <= receive_chunk_bytes <= 1_048_576:
            raise ValidationError("receive_chunk_bytes must be between 1 and 1048576")
        self._stream: ByteStream | None = stream
        self._receive_chunk_bytes = receive_chunk_bytes
        self._decoder = FrameDecoder()
        self._frames: deque[BridgeFrame] = deque()
        self._events: deque[BridgeEvent] = deque()
        self._next_request_id = secrets.randbits(63) or 1
        self._state_lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._request_lock = threading.Lock()

    @classmethod
    def connect_unix(
        cls,
        path: str,
        *,
        timeout_seconds: float = 3.0,
    ) -> NativeBridgeClient:
        if not isinstance(path, str) or not path:
            raise ValidationError("Unix-domain socket path must be non-empty")
        if not 0.1 <= timeout_seconds <= 60:
            raise ValidationError("timeout_seconds must be between 0.1 and 60")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout_seconds)
        try:
            sock.connect(path)
        except OSError as exc:
            sock.close()
            raise BridgeConnectionError(
                f"could not connect to native bridge socket {path!r}: {exc}"
            ) from exc
        return cls(sock)

    @property
    def connected(self) -> bool:
        with self._state_lock:
            return self._stream is not None

    def close(self) -> None:
        with self._state_lock:
            stream, self._stream = self._stream, None
            self._frames.clear()
            self._events.clear()
            self._decoder = FrameDecoder()
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def request(
        self,
        operation: str,
        params: Mapping[str, Any] | None = None,
        *,
        binary: bytes = b"",
        deadline_ms: int | None = None,
    ) -> BridgeResponse:
        if not isinstance(operation, str) or not operation or len(operation) > 128:
            raise ValidationError("operation must be a non-empty string up to 128 characters")
        if params is not None and not isinstance(params, Mapping):
            raise ValidationError("params must be a JSON object")
        if deadline_ms is not None and (
            isinstance(deadline_ms, bool)
            or not isinstance(deadline_ms, int)
            or not 1 <= deadline_ms <= 600_000
        ):
            raise ValidationError("deadline_ms must be between 1 and 600000")

        with self._request_lock:
            with self._state_lock:
                stream = self._require_connected_locked()
                request_id = self._take_request_id_locked()
            document: dict[str, Any] = {
                "operation": operation,
                "params": dict(params or {}),
            }
            if deadline_ms is not None:
                document["deadline_ms"] = deadline_ms
            payload = encode_frame(
                MessageType.REQUEST,
                request_id,
                document,
                binary=binary,
            )
            try:
                with self._send_lock:
                    stream.sendall(payload)
                while True:
                    frame = self._next_frame_locked()
                    if frame.message_type == MessageType.EVENT:
                        event = self._parse_event(frame)
                        with self._state_lock:
                            self._events.append(event)
                        continue
                    if frame.message_type != MessageType.RESPONSE:
                        self._fatal_protocol_locked(
                            f"expected response/event, received {frame.message_type.name.lower()}"
                        )
                    if frame.request_id != request_id:
                        self._fatal_protocol_locked(
                            f"response request_id {frame.request_id} does not match {request_id}"
                        )
                    return self._parse_response(frame)
            except BridgeProtocolError:
                self.close()
                raise
            except BridgeRemoteError:
                raise
            except OSError as exc:
                self.close()
                raise BridgeConnectionError(
                    f"native bridge connection failed during {operation!r}: {exc}"
                ) from exc

    def cancel(self, target_request_id: int) -> None:
        if (
            isinstance(target_request_id, bool)
            or not isinstance(target_request_id, int)
            or not 1 <= target_request_id <= 0xFFFF_FFFF_FFFF_FFFF
        ):
            raise ValidationError("target_request_id must be an unsigned 64-bit integer")
        with self._state_lock:
            stream = self._require_connected_locked()
            cancel_id = self._take_request_id_locked()
            payload = encode_frame(
                MessageType.CANCEL,
                cancel_id,
                {"target_request_id": target_request_id},
            )
            try:
                with self._send_lock:
                    stream.sendall(payload)
            except OSError as exc:
                self.close()
                raise BridgeConnectionError(
                    f"native bridge connection failed while cancelling: {exc}"
                ) from exc

    def drain_events(self) -> list[BridgeEvent]:
        with self._state_lock:
            events = list(self._events)
            self._events.clear()
            return events

    def _next_frame_locked(self) -> BridgeFrame:
        if self._frames:
            return self._frames.popleft()
        stream = self._require_connected_locked()
        while True:
            chunk = stream.recv(self._receive_chunk_bytes)
            if not chunk:
                self.close()
                raise BridgeConnectionError("native bridge closed the byte stream")
            frames = self._decoder.feed(chunk)
            if frames:
                self._frames.extend(frames[1:])
                return frames[0]

    def _parse_response(self, frame: BridgeFrame) -> BridgeResponse:
        ok = frame.document.get("ok")
        if not isinstance(ok, bool):
            self._fatal_protocol_locked("bridge response field 'ok' must be boolean")
        if ok:
            result = frame.document.get("result")
            if not isinstance(result, dict):
                self._fatal_protocol_locked("successful bridge response requires object result")
            return BridgeResponse(frame.request_id, result, frame.binary)

        error = frame.document.get("error")
        if not isinstance(error, dict):
            self._fatal_protocol_locked("failed bridge response requires object error")
        code = error.get("code")
        message = error.get("message")
        if not isinstance(code, str) or not code or not isinstance(message, str):
            self._fatal_protocol_locked("bridge error requires string code and message")
        raise BridgeRemoteError(code, message, error.get("details"))

    def _parse_event(self, frame: BridgeFrame) -> BridgeEvent:
        name = frame.document.get("event")
        sequence = frame.document.get("event_sequence")
        data = frame.document.get("data")
        if not isinstance(name, str) or not name:
            self._fatal_protocol_locked("bridge event requires a non-empty event name")
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence < 0
        ):
            self._fatal_protocol_locked("bridge event_sequence must be non-negative integer")
        if not isinstance(data, dict):
            self._fatal_protocol_locked("bridge event data must be an object")
        return BridgeEvent(name, sequence, data, frame.binary)

    def _take_request_id_locked(self) -> int:
        request_id = self._next_request_id
        self._next_request_id = (request_id + 1) & 0xFFFF_FFFF_FFFF_FFFF
        if self._next_request_id == 0:
            self._next_request_id = 1
        return request_id

    def _require_connected_locked(self) -> ByteStream:
        if self._stream is None:
            raise SessionError("native bridge is not connected")
        return self._stream

    def _fatal_protocol_locked(self, message: str) -> None:
        self.close()
        raise BridgeProtocolError(message)
