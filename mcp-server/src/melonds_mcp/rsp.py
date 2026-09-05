"""Small, synchronous client for melonDS' GDB Remote Serial Protocol stub.

melonDS uses an unusual but intentional initial handshake: the client sends a
single ``+`` after TCP connect and the stub answers with ``+``. Once connected,
the core is held in the debugger loop until a continue packet is received.
The implementation below models those details explicitly rather than relying
on a generic GDB process.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import select
import socket
import threading
from typing import Final

from .errors import (
    RspConnectionError,
    RspProtocolError,
    RspRemoteError,
    SessionError,
    ValidationError,
)
from .values import hex_u32


REGISTER_NAMES: Final[tuple[str, ...]] = (
    "r0",
    "r1",
    "r2",
    "r3",
    "r4",
    "r5",
    "r6",
    "r7",
    "r8",
    "r9",
    "r10",
    "r11",
    "r12",
    "sp",
    "lr",
    "pc",
    "cpsr",
    "sp_usr",
    "lr_usr",
    "r8_fiq",
    "r9_fiq",
    "r10_fiq",
    "r11_fiq",
    "r12_fiq",
    "sp_fiq",
    "lr_fiq",
    "sp_irq",
    "lr_irq",
    "sp_svc",
    "lr_svc",
    "sp_abt",
    "lr_abt",
    "sp_und",
    "lr_und",
    "spsr_fiq",
    "spsr_irq",
    "spsr_svc",
    "spsr_abt",
    "spsr_und",
)

_REGISTER_INDEX: Final[dict[str, int]] = {
    name: index for index, name in enumerate(REGISTER_NAMES)
}
_MAX_PACKET_BYTES: Final[int] = 4 * 1024 * 1024
_DEFAULT_CHUNK_BYTES: Final[int] = 512


class TargetState(str, Enum):
    DISCONNECTED = "disconnected"
    UNKNOWN = "unknown"
    RUNNING = "running"
    STOPPED = "stopped"
    EXITED = "exited"


_SIGNAL_NAMES: Final[dict[int, str]] = {
    2: "interrupt",
    4: "illegal_instruction",
    5: "trap",
    7: "emulation_trap",
    11: "segmentation_fault",
}


@dataclass(frozen=True, slots=True)
class StopReply:
    raw: str
    kind: str
    signal: int | None = None
    reason: str | None = None
    details: dict[str, str] | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "raw": self.raw,
            "kind": self.kind,
        }
        if self.signal is not None:
            result["signal"] = self.signal
            result["reason"] = self.reason or f"signal_{self.signal}"
        if self.details:
            result["details"] = dict(self.details)
        return result


def parse_stop_reply(reply: str) -> StopReply:
    """Turn an S/T/W/X RSP stop packet into a stable JSON-friendly record."""
    if len(reply) < 3 or reply[0] not in "STWX":
        raise RspProtocolError(f"expected a stop reply, received {reply!r}")
    try:
        signal = int(reply[1:3], 16)
    except ValueError as exc:
        raise RspProtocolError(f"invalid stop signal in {reply!r}") from exc

    if reply[0] in "WX":
        return StopReply(
            raw=reply,
            kind="exited" if reply[0] == "W" else "terminated",
            signal=signal,
            reason=_SIGNAL_NAMES.get(signal, f"signal_{signal}"),
        )

    details: dict[str, str] = {}
    if reply[0] == "T" and len(reply) > 3:
        for field in reply[3:].strip(";").split(";"):
            if ":" in field:
                key, value = field.split(":", 1)
                details[key] = value
    return StopReply(
        raw=reply,
        kind="stopped",
        signal=signal,
        reason=_SIGNAL_NAMES.get(signal, f"signal_{signal}"),
        details=details or None,
    )


class RspClient:
    """Thread-safe connection to one melonDS ARM core's GDB stub."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        core: str,
        timeout: float = 3.0,
    ) -> None:
        self.host = host
        self.port = port
        self.core = core
        self.timeout = timeout
        self._socket: socket.socket | None = None
        self._lock = threading.RLock()
        self.state = TargetState.DISCONNECTED
        self.last_stop: StopReply | None = None
        self.stop_sequence = 0
        self.capabilities: dict[str, str | bool] = {}
        self._no_ack = False

    @property
    def connected(self) -> bool:
        return self._socket is not None and self.state != TargetState.DISCONNECTED

    def connect(self, *, halt_on_connect: bool = False) -> None:
        """Connect, negotiate capabilities, and either halt or release the core."""
        with self._lock:
            if self.connected:
                raise SessionError(f"{self.core} is already connected")
            try:
                sock = socket.create_connection(
                    (self.host, self.port), timeout=self.timeout
                )
                sock.settimeout(self.timeout)
                sock.sendall(b"+")
                peer_ack = sock.recv(1)
                if peer_ack != b"+":
                    raise RspProtocolError(
                        "melonDS initial handshake did not return '+'"
                    )
            except (OSError, RspProtocolError) as exc:
                try:
                    sock.close()  # type: ignore[possibly-undefined]
                except (OSError, UnboundLocalError):
                    pass
                if isinstance(exc, RspProtocolError):
                    raise
                raise RspConnectionError(
                    f"could not connect to {self.core} at {self.host}:{self.port}: {exc}"
                ) from exc

            self._socket = sock
            self.state = TargetState.UNKNOWN
            try:
                supported = self._command_locked(
                    "qSupported:qXfer:features:read+;QStartNoAckMode+"
                )
                self.capabilities = self._parse_capabilities(supported)
                if self.capabilities.get("QStartNoAckMode") is not True:
                    raise RspProtocolError(
                        "this melonDS build does not support required QStartNoAckMode"
                    )
                reply = self._command_locked("QStartNoAckMode")
                self._expect_ok(reply, "enable RSP no-ack mode")
                self._no_ack = True
                if halt_on_connect:
                    self._record_stop_locked(self._command_locked("?"))
                else:
                    # A newly attached melonDS stub holds its CPU in Enter().
                    # Explicit continue is required even when it was running.
                    self._command_locked("c", expect_reply=False)
                    self.state = TargetState.RUNNING
                    self.last_stop = None
            except Exception:
                self.close()
                raise

    def close(self) -> None:
        with self._lock:
            sock, self._socket = self._socket, None
            self.state = TargetState.DISCONNECTED
            self.last_stop = None
            self.stop_sequence = 0
            self.capabilities = {}
            self._no_ack = False
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass

    def snapshot(self, *, blocked_by: str | None = None) -> dict[str, object]:
        with self._lock:
            result: dict[str, object] = {
                "core": self.core,
                "connected": self.connected,
                "state": self.state.value,
                "endpoint": f"{self.host}:{self.port}",
            }
            if self.last_stop is not None:
                result["last_stop"] = self.last_stop.as_dict()
            if blocked_by is not None and self.core != blocked_by:
                result["blocked_by"] = blocked_by
            return result

    def pause(self) -> StopReply:
        with self._lock:
            self._require_connected_locked()
            if self.state == TargetState.STOPPED and self.last_stop is not None:
                return self.last_stop
            if self._poll_stop_locked() and self.last_stop is not None:
                return self.last_stop
            assert self._socket is not None
            try:
                self._socket.sendall(b"\x03")
                reply = self._recv_packet_locked()
            except OSError as exc:
                self._connection_failed_locked(exc)
            stop = self._record_stop_locked(reply)
            # If an execution breakpoint became visible between the zero-time
            # poll above and Ctrl-C, melonDS may emit the breakpoint stop and a
            # second coalesced break stop. The target cannot run between them;
            # consume redundant stop packets now so the next command starts on
            # a clean packet boundary while preserving the first/causal reason.
            self._pause_barrier_locked()
            return stop

    def resume(self, *, address: int | None = None) -> None:
        with self._lock:
            self._require_stopped_locked()
            command = "c" if address is None else f"c{address:08x}"
            self._command_locked(command, expect_reply=False)
            self.state = TargetState.RUNNING
            self.last_stop = None

    def step(self, *, address: int | None = None) -> StopReply:
        with self._lock:
            self._require_stopped_locked()
            command = "s" if address is None else f"s{address:08x}"
            reply = self._command_locked(command)
            return self._record_stop_locked(reply)

    def poll_stop(self) -> StopReply | None:
        with self._lock:
            self._require_connected_locked()
            self._poll_stop_locked()
            return self.last_stop if self.state != TargetState.RUNNING else None

    def read_registers(self) -> dict[str, int]:
        with self._lock:
            self._require_stopped_locked()
            reply = self._command_locked("g")
            expected = len(REGISTER_NAMES) * 8
            if len(reply) != expected:
                raise RspProtocolError(
                    f"register packet has {len(reply)} hex digits; expected {expected}"
                )
            try:
                values = [
                    int.from_bytes(bytes.fromhex(reply[i : i + 8]), "little")
                    for i in range(0, len(reply), 8)
                ]
            except ValueError as exc:
                raise RspProtocolError("register packet is not hexadecimal") from exc
            return dict(zip(REGISTER_NAMES, values, strict=True))

    def read_register(self, name: str) -> int:
        canonical = name.strip().lower()
        try:
            index = _REGISTER_INDEX[canonical]
        except KeyError as exc:
            raise ValidationError(
                f"unknown register {name!r}; valid names: {', '.join(REGISTER_NAMES)}"
            ) from exc
        with self._lock:
            self._require_stopped_locked()
            reply = self._command_locked(f"p{index:x}")
            if len(reply) != 8:
                raise RspProtocolError(
                    f"register {canonical} returned malformed value {reply!r}"
                )
            try:
                return int.from_bytes(bytes.fromhex(reply), "little")
            except ValueError as exc:
                raise RspProtocolError(
                    f"register {canonical} returned non-hex value {reply!r}"
                ) from exc

    def write_register(self, name: str, value: int) -> None:
        canonical = name.strip().lower()
        try:
            index = _REGISTER_INDEX[canonical]
        except KeyError as exc:
            raise ValidationError(
                f"unknown register {name!r}; valid names: {', '.join(REGISTER_NAMES)}"
            ) from exc
        encoded = value.to_bytes(4, "little").hex()
        with self._lock:
            self._require_stopped_locked()
            reply = self._command_locked(f"P{index:x}={encoded}")
            self._expect_ok(reply, f"write register {canonical}")

    def read_memory(self, address: int, length: int) -> bytes:
        with self._lock:
            self._require_stopped_locked()
            output = bytearray()
            cursor = address
            remaining = length
            while remaining:
                chunk_size = min(remaining, _DEFAULT_CHUNK_BYTES)
                reply = self._command_locked(f"m{cursor:08x},{chunk_size:08x}")
                if len(reply) != chunk_size * 2:
                    raise RspProtocolError(
                        f"memory read at {hex_u32(cursor)} returned "
                        f"{len(reply) // 2} bytes; expected {chunk_size}"
                    )
                try:
                    output.extend(bytes.fromhex(reply))
                except ValueError as exc:
                    raise RspProtocolError(
                        f"memory read at {hex_u32(cursor)} was not hexadecimal"
                    ) from exc
                cursor = (cursor + chunk_size) & 0xFFFF_FFFF
                remaining -= chunk_size
            return bytes(output)

    def write_memory(self, address: int, data: bytes) -> None:
        with self._lock:
            self._require_stopped_locked()
            cursor = address
            offset = 0
            while offset < len(data):
                chunk = data[offset : offset + _DEFAULT_CHUNK_BYTES]
                reply = self._command_locked(
                    f"M{cursor:08x},{len(chunk):08x}:{chunk.hex()}"
                )
                self._expect_ok(reply, f"write memory at {hex_u32(cursor)}")
                cursor = (cursor + len(chunk)) & 0xFFFF_FFFF
                offset += len(chunk)

    def set_breakpoint(self, address: int, *, kind: int, enabled: bool) -> None:
        if kind not in (2, 4):
            raise ValidationError("breakpoint kind must be 2 (Thumb) or 4 (ARM)")
        with self._lock:
            self._require_stopped_locked()
            if self.capabilities.get("hwbreak") is not True:
                raise RspProtocolError(
                    "melonDS did not advertise required hardware breakpoint support"
                )
            command = "Z" if enabled else "z"
            reply = self._command_locked(f"{command}1,{address:x},{kind}")
            self._expect_ok(reply, "update breakpoint")

    def set_watchpoint(
        self,
        address: int,
        *,
        length: int,
        access: str,
        enabled: bool,
    ) -> None:
        watch_types = {"write": 2, "read": 3, "access": 4}
        try:
            watch_type = watch_types[access]
        except KeyError as exc:
            raise ValidationError("watchpoint access must be read, write, or access") from exc
        with self._lock:
            self._require_stopped_locked()
            command = "Z" if enabled else "z"
            reply = self._command_locked(
                f"{command}{watch_type},{address:x},{length}"
            )
            self._expect_ok(reply, "update watchpoint")

    def monitor(self, command: str) -> str:
        encoded = command.encode("utf-8").hex()
        with self._lock:
            self._require_stopped_locked()
            return self._command_locked(f"qRcmd,{encoded}")

    def _command_locked(
        self,
        payload: str,
        *,
        expect_reply: bool = True,
        allow_interleaved_stops: bool = False,
    ) -> str:
        self._require_connected_locked()
        try:
            payload_bytes = payload.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValidationError("RSP commands must contain ASCII characters") from exc
        packet = b"$" + payload_bytes + b"#" + f"{sum(payload_bytes) & 0xFF:02x}".encode()
        if len(packet) > _MAX_PACKET_BYTES:
            raise ValidationError("RSP request exceeds the local packet safety limit")

        assert self._socket is not None
        try:
            attempts = 1 if self._no_ack else 3
            for _attempt in range(attempts):
                self._socket.sendall(packet)
                if self._no_ack:
                    break
                ack = self._socket.recv(1)
                if ack == b"+":
                    break
                if ack != b"-":
                    raise RspProtocolError(
                        f"expected RSP acknowledgement, received {ack!r}"
                    )
            else:
                raise RspProtocolError("melonDS rejected the RSP packet three times")

            if not expect_reply:
                return ""
            reply = self._recv_packet_locked()
            interleaved_stops = 0
            while allow_interleaved_stops and reply[:1] in "STWX":
                parse_stop_reply(reply)
                interleaved_stops += 1
                if interleaved_stops > 16:
                    raise RspProtocolError(
                        "too many coalesced stop packets before barrier response"
                    )
                reply = self._recv_packet_locked()
        except OSError as exc:
            self._connection_failed_locked(exc)

        if reply.startswith("E"):
            raise RspRemoteError(
                f"melonDS rejected RSP command {payload.split(':', 1)[0]!r}: {reply}"
            )
        return reply

    def _recv_packet_locked(self) -> str:
        assert self._socket is not None
        encoded = bytearray()
        try:
            while True:
                marker = self._socket.recv(1)
                if marker == b"":
                    raise RspConnectionError("melonDS closed the debugger connection")
                if marker in (b"+", b"-"):
                    continue
                if marker == b"$":
                    break
                if marker == b"\x04":
                    raise RspConnectionError("melonDS ended the debugger connection")
                raise RspProtocolError(
                    f"expected start of RSP response, received {marker!r}"
                )

            while True:
                byte = self._socket.recv(1)
                if byte == b"":
                    raise RspConnectionError("melonDS closed an incomplete RSP packet")
                if byte == b"#":
                    break
                encoded.extend(byte)
                if len(encoded) > _MAX_PACKET_BYTES:
                    raise RspProtocolError("RSP response exceeds the packet safety limit")

            checksum_text = self._recv_exact_locked(2)
            try:
                expected_checksum = int(checksum_text, 16)
            except ValueError as exc:
                if not self._no_ack:
                    self._socket.sendall(b"-")
                error = RspProtocolError("RSP response has an invalid checksum")
                self._connection_failed_locked(error)
                raise AssertionError("unreachable") from exc
            actual_checksum = sum(encoded) & 0xFF
            if expected_checksum != actual_checksum:
                if not self._no_ack:
                    self._socket.sendall(b"-")
                error = RspProtocolError(
                    f"RSP checksum mismatch: got {actual_checksum:02x}, "
                    f"expected {expected_checksum:02x}"
                )
                self._connection_failed_locked(error)
            if not self._no_ack:
                self._socket.sendall(b"+")
        except OSError as exc:
            self._connection_failed_locked(exc)

        decoded = bytearray()
        escaped = False
        for byte in encoded:
            if escaped:
                decoded.append(byte ^ 0x20)
                escaped = False
            elif byte == ord("}"):
                escaped = True
            else:
                decoded.append(byte)
        if escaped:
            raise RspProtocolError("RSP response ends with an incomplete escape")
        try:
            return decoded.decode("ascii")
        except UnicodeDecodeError as exc:
            raise RspProtocolError("RSP response is not ASCII") from exc

    def _recv_exact_locked(self, size: int) -> bytes:
        assert self._socket is not None
        output = bytearray()
        while len(output) < size:
            chunk = self._socket.recv(size - len(output))
            if not chunk:
                raise RspConnectionError("melonDS closed an incomplete RSP packet")
            output.extend(chunk)
        return bytes(output)

    def _poll_stop_locked(self) -> bool:
        if self._socket is None or self.state != TargetState.RUNNING:
            return False
        try:
            readable, _, _ = select.select([self._socket], [], [], 0)
        except (OSError, ValueError) as exc:
            self._connection_failed_locked(exc)
        if not readable:
            return False
        self._record_stop_locked(self._recv_packet_locked())
        return True

    def _pause_barrier_locked(self) -> None:
        """Order a query after Ctrl-C and consume all earlier stop packets."""

        response = self._command_locked(
            "qSupported:qXfer:features:read+;QStartNoAckMode+",
            allow_interleaved_stops=True,
        )
        capabilities = self._parse_capabilities(response)
        if capabilities.get("QStartNoAckMode") is not True:
            raise RspProtocolError("pause barrier returned invalid qSupported response")

    def _record_stop_locked(self, reply: str) -> StopReply:
        stop = parse_stop_reply(reply)
        self.last_stop = stop
        self.stop_sequence += 1
        self.state = (
            TargetState.EXITED if stop.kind in ("exited", "terminated") else TargetState.STOPPED
        )
        return stop

    def _require_connected_locked(self) -> None:
        if self._socket is None or self.state == TargetState.DISCONNECTED:
            raise SessionError(f"{self.core} is not connected")

    def _require_stopped_locked(self) -> None:
        self._require_connected_locked()
        if self.state == TargetState.RUNNING:
            self._poll_stop_locked()
        if self.state != TargetState.STOPPED:
            raise SessionError(
                f"{self.core} must be the active stopped core before this operation"
            )

    def _connection_failed_locked(self, exc: BaseException) -> None:
        endpoint = f"{self.host}:{self.port}"
        self.close()
        if isinstance(exc, (RspConnectionError, RspProtocolError)):
            raise exc
        raise RspConnectionError(f"connection to {endpoint} failed: {exc}") from exc

    @staticmethod
    def _expect_ok(reply: str, operation: str) -> None:
        if reply != "OK":
            raise RspProtocolError(f"{operation} returned unexpected reply {reply!r}")

    @staticmethod
    def _parse_capabilities(reply: str) -> dict[str, str | bool]:
        capabilities: dict[str, str | bool] = {}
        for item in reply.split(";"):
            if not item:
                continue
            if item.endswith("+"):
                capabilities[item[:-1]] = True
            elif item.endswith("-"):
                capabilities[item[:-1]] = False
            elif "=" in item:
                key, value = item.split("=", 1)
                capabilities[key] = value
            else:
                capabilities[item] = True
        return capabilities
