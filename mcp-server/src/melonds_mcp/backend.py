"""High-level, dual-core-safe melonDS debugger backend."""

from __future__ import annotations

import base64
import ipaddress
import threading
import uuid
from typing import Literal, TypeAlias

from .disassembly import disassemble_arm
from .errors import RspError, SessionError, ValidationError
from .rsp import REGISTER_NAMES, RspClient, TargetState
from .values import (
    hex_u32,
    parse_counter,
    parse_hex_bytes,
    parse_positive_int,
    parse_u32,
)


CoreName: TypeAlias = Literal["arm9", "arm7"]
CoreSelection: TypeAlias = Literal["arm9", "arm7", "both"]
DisassemblyMode: TypeAlias = Literal["auto", "arm", "thumb"]

# CPU-logical address windows audited as ordinary memory rather than command/
# FIFO/cartridge access. A requested range must fit wholly within one window.
# The native bridge will replace this coarse policy with named physical spaces
# and explicit debug_peek versus side-effectful bus_access operations.
_SAFE_MEMORY_WINDOWS: tuple[tuple[int, int], ...] = (
    (0x0000_0000, 0x00FF_FFFF),  # ARM7 BIOS / ARM9 ITCM and low mirrors
    (0x0200_0000, 0x03FF_FFFF),  # main RAM, shared WRAM, ARM7 WRAM
    (0x0500_0000, 0x07FF_FFFF),  # palette, VRAM, OAM
    (0xFFFF_0000, 0xFFFF_FFFF),  # ARM9 high BIOS window
)


class MelonDSBackend:
    """Owns debugger connections and serializes active-core transitions.

    melonDS polls both GDB stubs from the same emulation thread. A stopped core
    blocks that thread, so the other core cannot service its debugger socket.
    Every operation therefore activates exactly one core: if another core is
    stopped, it is resumed before the requested core is interrupted.
    """

    def __init__(
        self,
        *,
        max_memory_bytes: int = 65_536,
        allow_remote: bool = False,
    ) -> None:
        self.max_memory_bytes = max_memory_bytes
        self.allow_remote = allow_remote
        self._clients: dict[CoreName, RspClient] = {}
        self._active_core: CoreName | None = None
        self._server_instance_id = str(uuid.uuid4())
        self._session_id: str | None = None
        self._state_version = 0
        self._stop_id = 0
        self._known_stop_sequences: dict[CoreName, int] = {}
        self._breakpoints: dict[CoreName, dict[tuple[int, int], None]] = {}
        self._lock = threading.RLock()

    @property
    def active_core(self) -> CoreName | None:
        with self._lock:
            self._refresh_active_locked()
            return self._active_core

    def attach(
        self,
        *,
        host: str = "127.0.0.1",
        arm9_port: int = 3333,
        arm7_port: int = 3334,
        cores: CoreSelection = "both",
        active_core: Literal["arm9", "arm7", "none"] = "arm9",
        timeout_seconds: float = 3.0,
    ) -> dict[str, object]:
        with self._lock:
            if self._clients:
                raise SessionError("a melonDS debugger session is already attached")
            self._validate_host(host)
            self._validate_port(arm9_port, "arm9_port")
            self._validate_port(arm7_port, "arm7_port")
            if not 0.1 <= timeout_seconds <= 60:
                raise ValidationError("timeout_seconds must be between 0.1 and 60")
            selected = self._selection(cores)
            if active_core != "none" and active_core not in selected:
                raise ValidationError(
                    "active_core must be one of the cores selected for attachment"
                )

            ports: dict[CoreName, int] = {"arm9": arm9_port, "arm7": arm7_port}
            debugger_host = host.strip()
            connected: dict[CoreName, RspClient] = {}
            try:
                # Release each newly connected CPU before attaching the other.
                # Keeping the first CPU in Enter() would deadlock the second
                # connection because both are polled on the emulation thread.
                for core in selected:
                    client = RspClient(
                        debugger_host,
                        ports[core],
                        core=core,
                        timeout=timeout_seconds,
                    )
                    client.connect(halt_on_connect=False)
                    connected[core] = client
                self._clients = connected
                self._session_id = str(uuid.uuid4())
                self._known_stop_sequences = {core: 0 for core in connected}
                self._breakpoints = {core: {} for core in connected}
                self._state_version += 1
                if active_core != "none":
                    self._activate_locked(active_core)
            except Exception:
                for client in connected.values():
                    client.close()
                self._clients = {}
                self._active_core = None
                self._session_id = None
                self._known_stop_sequences = {}
                self._breakpoints = {}
                raise
            return self.status()

    def detach(
        self,
        *,
        expected_session_id: str,
        expected_stop_id: int | str,
        expected_state_version: int | str,
    ) -> dict[str, object]:
        with self._lock:
            self._refresh_active_locked()
            self._check_expected_snapshot_locked(
                expected_session_id,
                expected_stop_id,
                expected_state_version,
            )
            detached_session_id = self._session_id
            endpoints = [
                f"{client.host}:{client.port}" for client in self._clients.values()
            ]
            removed_breakpoints = 0
            # Stock melonDS historically retained debugger breakpoints after a
            # socket closed. Remove every breakpoint while each core is active;
            # the fork also clears them in GdbStub::Disconnect as crash defense.
            for core, breakpoints in self._breakpoints.items():
                if not breakpoints:
                    continue
                client = self._client_locked(core)
                if not client.connected:
                    continue
                self._activate_locked(core)
                for address, kind in tuple(breakpoints):
                    self._state_version += 1
                    client.set_breakpoint(address, kind=kind, enabled=False)
                    del breakpoints[(address, kind)]
                    removed_breakpoints += 1

            self._refresh_active_locked()
            if self._active_core is not None:
                active = self._client_locked(self._active_core)
                if active.state == TargetState.STOPPED:
                    self._state_version += 1
                    active.resume()
                self._active_core = None

            for client in self._clients.values():
                client.close()
            self._clients = {}
            self._active_core = None
            self._session_id = None
            self._known_stop_sequences = {}
            self._breakpoints = {}
            self._state_version += 1
            return {
                "attached": False,
                "detached_session_id": detached_session_id,
                "closed_endpoints": endpoints,
                "removed_breakpoints": removed_breakpoints,
            }

    def status(self) -> dict[str, object]:
        with self._lock:
            self._refresh_active_locked()
            return {
                "attached": bool(self._clients),
                "server_instance_id": self._server_instance_id,
                "session_id": self._session_id,
                "active_core": self._active_core,
                "state_version": str(self._state_version),
                "stop_id": str(self._stop_id),
                "consistency": (
                    "active_core_gdb_stop" if self._active_core else "running_or_detached"
                ),
                "single_active_core_required": True,
                "backend": "transitional_gdb_rsp",
                "cores": {
                    core: client.snapshot(blocked_by=self._active_core)
                    for core, client in self._clients.items()
                },
                "limits": {"max_memory_bytes": self.max_memory_bytes},
            }

    def activate_core(
        self,
        core: CoreName,
        *,
        expected_session_id: str,
        expected_stop_id: int | str,
        expected_state_version: int | str,
    ) -> dict[str, object]:
        with self._lock:
            self._refresh_active_locked()
            self._check_expected_snapshot_locked(
                expected_session_id,
                expected_stop_id,
                expected_state_version,
            )
            stop = self._activate_locked(core)
            return {
                "active_core": core,
                "stop": stop.as_dict(),
                "status": self.status(),
            }

    def pause(
        self,
        core: CoreName,
        *,
        expected_session_id: str,
        expected_stop_id: int | str,
        expected_state_version: int | str,
    ) -> dict[str, object]:
        return self.activate_core(
            core,
            expected_session_id=expected_session_id,
            expected_stop_id=expected_stop_id,
            expected_state_version=expected_state_version,
        )

    def resume(
        self,
        core: CoreName | None = None,
        *,
        expected_session_id: str,
        expected_stop_id: int | str,
        expected_state_version: int | str,
    ) -> dict[str, object]:
        with self._lock:
            self._refresh_active_locked()
            self._check_expected_snapshot_locked(
                expected_session_id,
                expected_stop_id,
                expected_state_version,
            )
            selected = core or self._active_core
            if selected is None:
                raise SessionError("no stopped core is active")
            client = self._client_locked(selected)
            if client.state != TargetState.STOPPED:
                raise SessionError(f"{selected} is not stopped")
            self._state_version += 1
            client.resume()
            if self._active_core == selected:
                self._active_core = None
            return self.status()

    def step(
        self,
        core: CoreName,
        *,
        count: int = 1,
        expected_session_id: str,
        expected_stop_id: int | str,
        expected_state_version: int | str,
    ) -> dict[str, object]:
        parse_positive_int(count, field="count", maximum=1_000)
        with self._lock:
            client = self._require_active_locked(core)
            self._check_expected_snapshot_locked(
                expected_session_id,
                expected_stop_id,
                expected_state_version,
            )
            stop = None
            for _ in range(count):
                self._state_version += 1
                stop = client.step()
                self._sync_stop_sequence_locked(core, client)
            assert stop is not None
            self._active_core = core
            registers = client.read_registers()
            return {
                "core": core,
                "steps": count,
                "stop": stop.as_dict(),
                "pc": hex_u32(registers["pc"]),
                "cpsr": hex_u32(registers["cpsr"]),
                "mode": "thumb" if registers["cpsr"] & 0x20 else "arm",
            }

    def read_registers(
        self,
        core: CoreName,
        *,
        names: list[str] | None = None,
    ) -> dict[str, object]:
        with self._lock:
            client = self._require_active_locked(core)
            values = client.read_registers()
            if names:
                canonical = [name.strip().lower() for name in names]
                unknown = [name for name in canonical if name not in values]
                if unknown:
                    raise ValidationError(
                        f"unknown register(s): {', '.join(unknown)}; "
                        f"valid names: {', '.join(REGISTER_NAMES)}"
                    )
                values = {name: values[name] for name in canonical}
            return {
                "core": core,
                "registers": {name: hex_u32(value) for name, value in values.items()},
            }

    def write_register(
        self,
        core: CoreName,
        *,
        name: str,
        value: int | str,
        expected_session_id: str,
        expected_stop_id: int | str,
        expected_state_version: int | str,
        verify: bool = True,
    ) -> dict[str, object]:
        parsed = parse_u32(value, field="value")
        canonical_name = name.strip().lower()
        if canonical_name == "cpsr" or canonical_name.startswith("spsr_"):
            raise ValidationError(
                "CPSR/SPSR writes are unavailable on the transitional GDB backend: "
                "upstream can alias these writes to CPSR without safely switching "
                "banked-register mode or rebuilding the instruction pipeline"
            )
        with self._lock:
            client = self._require_active_locked(core)
            self._check_expected_snapshot_locked(
                expected_session_id,
                expected_stop_id,
                expected_state_version,
            )
            self._state_version += 1
            client.write_register(canonical_name, parsed)
            result: dict[str, object] = {
                "core": core,
                "register": canonical_name,
                "written": hex_u32(parsed),
            }
            if verify:
                actual = client.read_register(canonical_name)
                result["verified"] = actual == parsed
                result["actual"] = hex_u32(actual)
                if actual != parsed:
                    raise RspError(
                        f"register verification failed: wrote {hex_u32(parsed)}, "
                        f"read {hex_u32(actual)}"
                    )
            return result

    def read_memory(
        self,
        core: CoreName,
        *,
        address: int | str,
        length: int,
        encoding: Literal["hex", "base64"] = "hex",
    ) -> dict[str, object]:
        start = parse_u32(address, field="address")
        self._validate_memory_range(start, length)
        self._validate_memory_policy(start, length, writing=False)
        if encoding not in ("hex", "base64"):
            raise ValidationError("encoding must be hex or base64")
        with self._lock:
            client = self._require_active_locked(core)
            data = client.read_memory(start, length)
            encoded = data.hex() if encoding == "hex" else base64.b64encode(data).decode()
            return {
                "core": core,
                "address": hex_u32(start),
                "length": len(data),
                "encoding": encoding,
                "data": encoded,
            }

    def write_memory(
        self,
        core: CoreName,
        *,
        address: int | str,
        data_hex: str,
        expected_session_id: str,
        expected_stop_id: int | str,
        expected_state_version: int | str,
        verify: bool = True,
    ) -> dict[str, object]:
        start = parse_u32(address, field="address")
        if not isinstance(data_hex, str):
            raise ValidationError("data_hex must be a hexadecimal string")
        max_text_length = self.max_memory_bytes * 3 + 2
        if len(data_hex) > max_text_length:
            raise ValidationError(
                "data_hex text is too large for the configured "
                f"{self.max_memory_bytes}-byte memory limit"
            )
        data = parse_hex_bytes(data_hex)
        self._validate_memory_range(start, len(data))
        self._validate_memory_policy(start, len(data), writing=True)
        with self._lock:
            client = self._require_active_locked(core)
            self._check_expected_snapshot_locked(
                expected_session_id,
                expected_stop_id,
                expected_state_version,
            )
            before = client.read_memory(start, len(data))
            # Conservatively invalidate every snapshot before the first write
            # attempt. Even a failed RSP command may have partially mutated the
            # target, and a rollback cannot make that interval externally atomic.
            self._state_version += 1
            try:
                client.write_memory(start, data)
                if verify:
                    actual = client.read_memory(start, len(data))
                    if actual != data:
                        mismatch = next(
                            index
                            for index, (expected, got) in enumerate(
                                zip(data, actual, strict=True)
                            )
                            if expected != got
                        )
                        raise RspError(
                            "memory verification failed at "
                            f"{hex_u32(start + mismatch)}: wrote {data[mismatch]:02x}, "
                            f"read {actual[mismatch]:02x}"
                        )
            except Exception as write_error:
                try:
                    client.write_memory(start, before)
                except Exception as rollback_error:
                    raise RspError(
                        f"memory write failed and rollback also failed: {rollback_error}"
                    ) from write_error
                raise
            result: dict[str, object] = {
                "core": core,
                "address": hex_u32(start),
                "bytes_written": len(data),
                "atomic": False,
                "rollback_strategy": "best_effort_before_image",
            }
            if verify:
                result["verified"] = True
            return result

    def set_breakpoint(
        self,
        core: CoreName,
        *,
        address: int | str,
        enabled: bool,
        mode: DisassemblyMode = "auto",
        expected_session_id: str,
        expected_stop_id: int | str,
        expected_state_version: int | str,
    ) -> dict[str, object]:
        parsed_address = parse_u32(address, field="address")
        with self._lock:
            client = self._require_active_locked(core)
            self._check_expected_snapshot_locked(
                expected_session_id,
                expected_stop_id,
                expected_state_version,
            )
            resolved_mode = self._resolve_mode_locked(client, mode)
            kind = 2 if resolved_mode == "thumb" else 4
            if resolved_mode == "arm" and parsed_address & 0x3:
                raise ValidationError("ARM breakpoints must be 4-byte aligned")
            canonical_address = (
                parsed_address & ~1 if resolved_mode == "thumb" else parsed_address
            )
            key = (canonical_address, kind)
            if enabled:
                # Track a possibly-installed breakpoint even if the response is
                # lost, so detach will attempt a defensive removal.
                self._breakpoints[core][key] = None
            self._state_version += 1
            client.set_breakpoint(canonical_address, kind=kind, enabled=enabled)
            if not enabled:
                self._breakpoints[core].pop(key, None)
            return {
                "core": core,
                "address": hex_u32(canonical_address),
                "enabled": enabled,
                "mode": resolved_mode,
                "kind": kind,
            }

    def set_watchpoint(
        self,
        core: CoreName,
        *,
        address: int | str,
        length: int,
        access: Literal["read", "write", "access"],
        enabled: bool,
        expected_session_id: str,
        expected_stop_id: int | str,
        expected_state_version: int | str,
    ) -> dict[str, object]:
        parsed_address = parse_u32(address, field="address")
        parse_positive_int(length, field="length", maximum=0x1_0000)
        if parsed_address + length - 1 > 0xFFFF_FFFF:
            raise ValidationError("watchpoint range crosses the 32-bit address boundary")
        with self._lock:
            client = self._require_active_locked(core)
            self._check_expected_snapshot_locked(
                expected_session_id,
                expected_stop_id,
                expected_state_version,
            )
            self._state_version += 1
            client.set_watchpoint(
                parsed_address,
                length=length,
                access=access,
                enabled=enabled,
            )
            return {
                "core": core,
                "address": hex_u32(parsed_address),
                "length": length,
                "access": access,
                "enabled": enabled,
            }

    def disassemble(
        self,
        core: CoreName,
        *,
        address: int | str | None = None,
        count: int = 16,
        mode: DisassemblyMode = "auto",
    ) -> dict[str, object]:
        parse_positive_int(count, field="count", maximum=256)
        with self._lock:
            client = self._require_active_locked(core)
            registers = client.read_registers()
            start = registers["pc"] if address is None else parse_u32(address, field="address")
            resolved_mode = self._resolve_mode_locked(client, mode, registers=registers)
            # Thumb mixes 16- and 32-bit instructions; four bytes per requested
            # instruction is sufficient for either mode and keeps the response bounded.
            read_size = count * 4
            if start + read_size - 1 > 0xFFFF_FFFF:
                read_size = 0x1_0000_0000 - start
            self._validate_memory_policy(start, read_size, writing=False)
            data = client.read_memory(start, read_size)
            instructions = disassemble_arm(
                data,
                address=start,
                mode=resolved_mode,
                count=count,
                isa="armv5te" if core == "arm9" else "armv4t",
            )
            return {
                "core": core,
                "cpu_isa": "armv5te" if core == "arm9" else "armv4t",
                "isa_validation": (
                    "thumb1_width_constrained"
                    if resolved_mode == "thumb"
                    else "capstone_arm_best_effort"
                ),
                "address": hex_u32(start),
                "mode": resolved_mode,
                "requested_count": count,
                "decoded_count": len(instructions),
                "instructions": instructions,
            }

    def _activate_locked(self, core: CoreName):
        client = self._client_locked(core)
        self._refresh_active_locked()
        if client.state == TargetState.STOPPED:
            self._active_core = core
            assert client.last_stop is not None
            return client.last_stop

        if self._active_core is not None and self._active_core != core:
            current = self._client_locked(self._active_core)
            if current.state == TargetState.STOPPED:
                self._state_version += 1
                current.resume()
            self._active_core = None

        self._state_version += 1
        stop = client.pause()
        self._active_core = core
        self._sync_stop_sequence_locked(core, client)
        return stop

    def _refresh_active_locked(self) -> None:
        stopped: list[CoreName] = []
        for core, client in self._clients.items():
            if client.state == TargetState.RUNNING:
                client.poll_stop()
            self._sync_stop_sequence_locked(core, client)
            if client.state == TargetState.STOPPED:
                stopped.append(core)
        if len(stopped) == 1:
            self._active_core = stopped[0]
        elif not stopped:
            self._active_core = None
        elif self._active_core not in stopped:
            # This should not occur with melonDS' single emulation thread, but
            # preserve a deterministic choice if a test/future bridge allows it.
            self._active_core = stopped[0]

    def _require_active_locked(self, core: CoreName) -> RspClient:
        self._refresh_active_locked()
        client = self._client_locked(core)
        if self._active_core != core or client.state != TargetState.STOPPED:
            current = self._active_core or "none"
            raise SessionError(
                f"{core} is not the active stopped core (active: {current}); "
                f"call emulator_activate_core first"
            )
        return client

    def _check_expected_stop_locked(self, expected_stop_id: int | str) -> None:
        expected = parse_counter(expected_stop_id, field="expected_stop_id")
        if expected != self._stop_id:
            raise SessionError(
                f"stale stop: expected {expected}, current stop_id is {self._stop_id}"
            )

    def _check_expected_snapshot_locked(
        self,
        expected_session_id: str,
        expected_stop_id: int | str,
        expected_state_version: int | str,
    ) -> None:
        if not isinstance(expected_session_id, str) or not expected_session_id:
            raise ValidationError("expected_session_id must be a non-empty string")
        if self._session_id is None or expected_session_id != self._session_id:
            raise SessionError(
                "stale session: expected "
                f"{expected_session_id!r}, current session_id is {self._session_id!r}"
            )
        self._check_expected_stop_locked(expected_stop_id)
        expected_state = parse_counter(
            expected_state_version,
            field="expected_state_version",
        )
        if expected_state != self._state_version:
            raise SessionError(
                "stale state: expected "
                f"{expected_state}, current state_version is {self._state_version}"
            )

    def _sync_stop_sequence_locked(self, core: CoreName, client: RspClient) -> None:
        known = self._known_stop_sequences.get(core, 0)
        if client.stop_sequence > known:
            delta = client.stop_sequence - known
            self._known_stop_sequences[core] = client.stop_sequence
            self._stop_id += delta
            self._state_version += delta

    def _client_locked(self, core: CoreName) -> RspClient:
        if core not in ("arm9", "arm7"):
            raise ValidationError("core must be arm9 or arm7")
        try:
            return self._clients[core]
        except KeyError as exc:
            raise SessionError(f"{core} is not attached") from exc

    def _resolve_mode_locked(
        self,
        client: RspClient,
        mode: DisassemblyMode,
        *,
        registers: dict[str, int] | None = None,
    ) -> Literal["arm", "thumb"]:
        if mode in ("arm", "thumb"):
            return mode
        if mode != "auto":
            raise ValidationError("mode must be auto, arm, or thumb")
        current = registers or client.read_registers()
        return "thumb" if current["cpsr"] & 0x20 else "arm"

    def _validate_memory_range(self, address: int, length: int) -> None:
        parse_positive_int(
            length,
            field="length",
            maximum=self.max_memory_bytes,
        )
        if address + length - 1 > 0xFFFF_FFFF:
            raise ValidationError("memory range crosses the 32-bit address boundary")

    def _validate_memory_policy(self, address: int, length: int, *, writing: bool) -> None:
        end = address + length - 1
        if any(address >= start and end <= window_end for start, window_end in _SAFE_MEMORY_WINDOWS):
            return
        operation = "writes" if writing else "reads"
        raise ValidationError(
            f"memory {operation} outside the transitional side-effect-free allowlist "
            "are denied; MMIO, cartridge ROM/SRAM/GPIO, and unaudited mappings require "
            "the native bridge"
        )

    def _validate_host(self, host: str) -> None:
        if not isinstance(host, str) or not host.strip():
            raise ValidationError("host must be a non-empty string")
        if self.allow_remote:
            return
        normalized = host.strip().lower()
        if normalized == "localhost":
            return
        try:
            if ipaddress.ip_address(normalized).is_loopback:
                return
        except ValueError:
            pass
        raise ValidationError(
            "remote debugger hosts are disabled; use a loopback address or set "
            "MELONDS_MCP_ALLOW_REMOTE=1 when starting the server"
        )

    @staticmethod
    def _validate_port(port: int, field: str) -> None:
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65_535:
            raise ValidationError(f"{field} must be an integer between 1 and 65535")

    @staticmethod
    def _selection(cores: CoreSelection) -> tuple[CoreName, ...]:
        if cores == "both":
            return ("arm9", "arm7")
        if cores in ("arm9", "arm7"):
            return (cores,)
        raise ValidationError("cores must be arm9, arm7, or both")
