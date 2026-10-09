"""Deterministic fake for melonDS' small GDB RSP dialect."""

from __future__ import annotations

import socket
import threading
import time

from melonds_mcp.rsp import REGISTER_NAMES


class FakeRspServer:
    def __init__(
        self,
        *,
        bad_checksum_for: str | None = None,
        coalesced_stop_on_interrupt: bool = False,
        coalesced_stop_delay: float = 0,
    ) -> None:
        self.bad_checksum_for = bad_checksum_for
        self.coalesced_stop_on_interrupt = coalesced_stop_on_interrupt
        self.coalesced_stop_delay = coalesced_stop_delay
        self.registers = [0] * len(REGISTER_NAMES)
        self.memory: dict[int, int] = {}
        self.commands: list[str] = []
        self.running = False
        self.no_ack = False
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self._listener.settimeout(0.2)
        self.port = self._listener.getsockname()[1]
        self._connection: socket.socket | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.error: BaseException | None = None

    def start(self) -> "FakeRspServer":
        self._thread.start()
        self._ready.wait(timeout=2)
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._connection is not None:
            try:
                self._connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._connection.close()
        self._listener.close()
        self._thread.join(timeout=2)
        if self.error is not None:
            raise self.error

    def __enter__(self) -> "FakeRspServer":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.stop()
        except OSError:
            pass

    def set_memory(self, address: int, data: bytes) -> None:
        for offset, value in enumerate(data):
            self.memory[address + offset] = value

    def get_memory(self, address: int, length: int) -> bytes:
        return bytes(self.memory.get(address + offset, 0) for offset in range(length))

    def _run(self) -> None:
        self._ready.set()
        try:
            while not self._stop.is_set():
                connection = None
                while connection is None and not self._stop.is_set():
                    try:
                        connection, _ = self._listener.accept()
                    except socket.timeout:
                        continue
                if connection is None:
                    return
                self._connection = connection
                self.no_ack = False
                connection.settimeout(0.2)
                if self._recv_exact(connection, 1) != b"+":
                    raise AssertionError("fake expected melonDS initial '+' handshake")
                connection.sendall(b"+")

                disconnected = False
                while not self._stop.is_set() and not disconnected:
                    try:
                        marker = connection.recv(1)
                    except socket.timeout:
                        continue
                    if not marker:
                        disconnected = True
                        continue
                    if marker in (b"+", b"-"):
                        continue
                    if marker == b"\x03":
                        self.commands.append("<interrupt>")
                        self.running = False
                        if self.coalesced_stop_on_interrupt:
                            self._send_response(connection, "S05", command="<breakpoint>")
                            time.sleep(self.coalesced_stop_delay)
                        self._send_response(connection, "S02", command="<interrupt>")
                        continue
                    if marker != b"$":
                        raise AssertionError(f"unexpected fake RSP marker {marker!r}")
                    payload = bytearray()
                    while True:
                        byte = self._recv_exact(connection, 1)
                        if byte == b"#":
                            break
                        payload.extend(byte)
                    checksum = self._recv_exact(connection, 2)
                    expected = f"{sum(payload) & 0xFF:02x}".encode()
                    if checksum.lower() != expected:
                        connection.sendall(b"-")
                        continue
                    command = payload.decode("ascii")
                    self.commands.append(command)
                    if not self.no_ack:
                        connection.sendall(b"+")
                    response = self._handle(command)
                    if response is not None:
                        self._send_response(connection, response, command=command)
                try:
                    connection.close()
                except OSError:
                    pass
                if self._connection is connection:
                    self._connection = None
        except (ConnectionError, OSError):
            if not self._stop.is_set():
                self.error = RuntimeError("fake RSP server connection failed unexpectedly")
        except BaseException as exc:  # surfaced by stop()
            self.error = exc

    def _handle(self, command: str) -> str | None:
        if command.startswith("qSupported"):
            return (
                "PacketSize=47B;qXfer:features:read+;hwbreak+;"
                "QStartNoAckMode+"
            )
        if command == "QStartNoAckMode":
            self.no_ack = True
            return "OK"
        if command == "?":
            self.running = False
            return "S02"
        if command.startswith("c"):
            self.running = True
            return None
        if command.startswith("s"):
            cpsr = self.registers[16]
            self.registers[15] = (
                self.registers[15] + (2 if cpsr & 0x20 else 4)
            ) & 0xFFFF_FFFF
            self.running = False
            return "S05"
        if command == "g":
            return "".join(value.to_bytes(4, "little").hex() for value in self.registers)
        if command.startswith("p"):
            index = int(command[1:], 16)
            return self.registers[index].to_bytes(4, "little").hex()
        if command.startswith("P"):
            register, encoded = command[1:].split("=", 1)
            self.registers[int(register, 16)] = int.from_bytes(
                bytes.fromhex(encoded), "little"
            )
            return "OK"
        if command.startswith("m"):
            address_text, length_text = command[1:].split(",", 1)
            return self.get_memory(int(address_text, 16), int(length_text, 16)).hex()
        if command.startswith("M"):
            location, encoded = command[1:].split(":", 1)
            address_text, length_text = location.split(",", 1)
            data = bytes.fromhex(encoded)
            assert len(data) == int(length_text, 16)
            self.set_memory(int(address_text, 16), data)
            return "OK"
        if command.startswith(("Z", "z")):
            return "OK"
        if command.startswith("qRcmd,"):
            return "OK"
        return ""

    def _send_response(self, connection: socket.socket, response: str, *, command: str) -> None:
        payload = response.encode("ascii")
        checksum = sum(payload) & 0xFF
        if self.bad_checksum_for is not None and command.startswith(self.bad_checksum_for):
            checksum = (checksum + 1) & 0xFF
        connection.sendall(b"$" + payload + b"#" + f"{checksum:02x}".encode())
        if self.no_ack:
            return
        ack = self._recv_exact(connection, 1)
        if ack not in (b"+", b"-"):
            raise AssertionError(f"fake expected response ack, received {ack!r}")

    @staticmethod
    def _recv_exact(connection: socket.socket, size: int) -> bytes:
        output = bytearray()
        while len(output) < size:
            chunk = connection.recv(size - len(output))
            if not chunk:
                raise ConnectionError("peer disconnected")
            output.extend(chunk)
        return bytes(output)
