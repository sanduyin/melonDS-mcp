"""MCP tool surface for the transitional melonDS GDB backend."""

from __future__ import annotations

from functools import wraps
import os
from typing import Annotated, Any, Callable, Literal, ParamSpec, TypedDict, TypeVar

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field, StrictBool, StrictInt, StrictStr

from .backend import MelonDSBackend
from .errors import (
    MelonDSMCPError,
    RspConnectionError,
    RspError,
    SessionError,
    ValidationError,
)


P = ParamSpec("P")
R = TypeVar("R")
CounterText = Annotated[StrictStr, Field(max_length=20, pattern=r"^[0-9]+$")]
CounterInput = StrictInt | CounterText
U32Text = Annotated[StrictStr, Field(max_length=32)]
U32Input = StrictInt | U32Text
SessionId = Annotated[StrictStr, Field(min_length=1, max_length=128)]
Host = Annotated[StrictStr, Field(min_length=1, max_length=253)]
RegisterName = Annotated[StrictStr, Field(min_length=1, max_length=32)]
Port = Annotated[StrictInt, Field(ge=1, le=65_535)]
TimeoutSeconds = Annotated[float, Field(strict=True, ge=0.1, le=60)]
StepCount = Annotated[StrictInt, Field(ge=1, le=1_000)]
MemoryLength = Annotated[StrictInt, Field(ge=1, le=1_048_576)]
InstructionCount = Annotated[StrictInt, Field(ge=1, le=256)]
RegisterNames = Annotated[list[RegisterName], Field(max_length=39)]
HexPayload = Annotated[StrictStr, Field(max_length=2_097_152)]


class Snapshot(TypedDict):
    attached: bool
    active_core: str | None
    server_instance_id: str
    session_id: str | None
    state_version: str
    stop_id: str
    consistency: str
    backend: str


class ToolEnvelope(TypedDict):
    snapshot: Snapshot
    data: Any


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_max_memory() -> int:
    raw = os.environ.get("MELONDS_MCP_MAX_MEMORY_BYTES", "65536")
    try:
        parsed = int(raw, 10)
    except ValueError:
        return 65_536
    return min(max(parsed, 1), 1_048_576)


class StrictMCPServer(MCPServer):
    """MCPServer variant that rejects unknown tool arguments fail-closed."""

    def add_tool(self, fn: Callable[..., Any], **kwargs: Any) -> None:  # type: ignore[override]
        super().add_tool(fn, **kwargs)
        tool_name = kwargs.get("name") or fn.__name__
        tool = self._tool_manager.get_tool(tool_name)
        if tool is None:  # pragma: no cover - registration invariant
            raise RuntimeError(f"tool registration failed for {tool_name}")
        argument_model = tool.fn_metadata.arg_model
        argument_model.model_config = {
            **argument_model.model_config,
            "extra": "forbid",
        }
        argument_model.model_rebuild(force=True)
        tool.parameters = argument_model.model_json_schema(by_alias=True)


backend = MelonDSBackend(
    max_memory_bytes=_env_max_memory(),
    allow_remote=_env_flag("MELONDS_MCP_ALLOW_REMOTE"),
)

mcp = StrictMCPServer(
    "melonDS MCP",
    log_level=os.environ.get("MELONDS_MCP_LOG_LEVEL", "INFO"),
)


READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=False,
)
EXECUTION = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=False,
)


def _tool_errors(function: Callable[P, R]) -> Callable[P, R]:
    """Give expected failures stable, model-readable error-code prefixes."""

    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return function(*args, **kwargs)
        except ValidationError as exc:
            raise ToolError(f"INVALID_ARGUMENT: {exc}") from exc
        except SessionError as exc:
            message = str(exc)
            if message.startswith("stale stop:"):
                code = "STALE_STOP"
            elif message.startswith("stale state:"):
                code = "STALE_STATE"
            elif message.startswith("stale session:"):
                code = "STALE_SESSION"
            else:
                code = "SESSION_STATE"
            raise ToolError(f"{code}: {exc}") from exc
        except RspConnectionError as exc:
            raise ToolError(f"DEBUGGER_CONNECTION: {exc}") from exc
        except RspError as exc:
            raise ToolError(f"DEBUGGER_PROTOCOL: {exc}") from exc
        except MelonDSMCPError as exc:
            raise ToolError(f"MELONDS_ERROR: {exc}") from exc

    return wrapped


def _snapshot(status: dict[str, object]) -> Snapshot:
    return {
        "attached": status["attached"],
        "active_core": status["active_core"],
        "server_instance_id": status["server_instance_id"],
        "session_id": status["session_id"],
        "state_version": status["state_version"],
        "stop_id": status["stop_id"],
        "consistency": status["consistency"],
        "backend": status["backend"],
    }


def _result(
    data: Any,
    *,
    status: dict[str, object] | None = None,
) -> ToolEnvelope:
    resolved_status = backend.status() if status is None else status
    return {"snapshot": _snapshot(resolved_status), "data": data}


@mcp.tool(
    name="emulator_status",
    title="Get melonDS debugger status",
    description=(
        "Inspect attachment state, active stopped CPU, monotonic stop/state counters, "
        "and per-core debugger state. Call this before any state-dependent operation."
    ),
    annotations=READ_ONLY,
)
@_tool_errors
def emulator_status() -> ToolEnvelope:
    status = backend.status()
    return _result(status, status=status)


@mcp.tool(
    name="emulator_attach",
    title="Attach to melonDS",
    description=(
        "Attach to melonDS' built-in ARM GDB ports. JIT must be disabled and the "
        "GDB stub enabled in melonDS. Both cores are connected without deadlocking, "
        "then active_core is explicitly stopped. Loopback hosts are required unless "
        "the server operator opted into remote connections."
    ),
    annotations=EXECUTION,
)
@_tool_errors
def emulator_attach(
    host: Host = "127.0.0.1",
    arm9_port: Port = 3333,
    arm7_port: Port = 3334,
    cores: Literal["arm9", "arm7", "both"] = "both",
    active_core: Literal["arm9", "arm7", "none"] = "arm9",
    timeout_seconds: TimeoutSeconds = 3.0,
) -> ToolEnvelope:
    status = backend.attach(
        host=host,
        arm9_port=arm9_port,
        arm7_port=arm7_port,
        cores=cores,
        active_core=active_core,
        timeout_seconds=timeout_seconds,
    )
    return _result(status, status=status)


@mcp.tool(
    name="emulator_detach",
    title="Detach from melonDS",
    description=(
        "Close all debugger sockets without terminating melonDS. Closing an active "
        "stopped socket releases the emulator from its GDB loop. All tracked "
        "breakpoints are removed first. Requires the complete expected snapshot."
    ),
    annotations=EXECUTION,
)
@_tool_errors
def emulator_detach(
    expected_session_id: SessionId,
    expected_stop_id: CounterInput,
    expected_state_version: CounterInput,
) -> ToolEnvelope:
    return _result(
        backend.detach(
            expected_session_id=expected_session_id,
            expected_stop_id=expected_stop_id,
            expected_state_version=expected_state_version,
        )
    )


@mcp.tool(
    name="emulator_activate_core",
    title="Pause and activate one DS CPU",
    description=(
        "Make ARM9 or ARM7 the sole active stopped debugger core. If the other core "
        "is stopped it is resumed first. This explicit transition is required before "
        "register, memory, breakpoint, step, or disassembly tools target a core. "
        "Requires expected_session_id, expected_stop_id, and expected_state_version."
    ),
    annotations=EXECUTION,
)
@_tool_errors
def emulator_activate_core(
    core: Literal["arm9", "arm7"],
    expected_session_id: SessionId,
    expected_stop_id: CounterInput,
    expected_state_version: CounterInput,
) -> ToolEnvelope:
    data = backend.activate_core(
        core,
        expected_session_id=expected_session_id,
        expected_stop_id=expected_stop_id,
        expected_state_version=expected_state_version,
    )
    return _result(data, status=data["status"])


@mcp.tool(
    name="emulator_resume",
    title="Resume the active DS CPU",
    description=(
        "Resume the active stopped core and return immediately. Use emulator_status "
        "to observe a later breakpoint stop. Requires the complete expected snapshot "
        "so a newly-arrived breakpoint cannot be resumed by a stale Agent action."
    ),
    annotations=EXECUTION,
)
@_tool_errors
def emulator_resume(
    core: Literal["arm9", "arm7"] | None = None,
    *,
    expected_session_id: SessionId,
    expected_stop_id: CounterInput,
    expected_state_version: CounterInput,
) -> ToolEnvelope:
    status = backend.resume(
        core,
        expected_session_id=expected_session_id,
        expected_stop_id=expected_stop_id,
        expected_state_version=expected_state_version,
    )
    return _result(status, status=status)


@mcp.tool(
    name="cpu_step",
    title="Single-step ARM instructions",
    description=(
        "Execute one or more instructions on the already active stopped core. "
        "All expected snapshot fields must match, preventing a stale or concurrent "
        "Agent decision from changing a newer state. Interpreter/GDB mode is required."
    ),
    annotations=EXECUTION,
)
@_tool_errors
def cpu_step(
    core: Literal["arm9", "arm7"],
    expected_session_id: SessionId,
    expected_stop_id: CounterInput,
    expected_state_version: CounterInput,
    count: StepCount = 1,
) -> ToolEnvelope:
    return _result(
        backend.step(
            core,
            count=count,
            expected_session_id=expected_session_id,
            expected_stop_id=expected_stop_id,
            expected_state_version=expected_state_version,
        )
    )


@mcp.tool(
    name="cpu_register_read",
    title="Read ARM registers",
    description=(
        "Read all or selected core/banked registers from the active stopped CPU. "
        "PC is melonDS' pipeline-corrected instruction address. Values are returned "
        "as fixed-width hexadecimal strings."
    ),
    annotations=READ_ONLY,
)
@_tool_errors
def cpu_register_read(
    core: Literal["arm9", "arm7"],
    names: RegisterNames | None = None,
) -> ToolEnvelope:
    return _result(backend.read_registers(core, names=names))


@mcp.tool(
    name="cpu_register_write",
    title="Write one ARM register",
    description=(
        "Write and optionally verify one register on the active stopped CPU. "
        "Requires the complete expected snapshot. Writing PC uses melonDS' pipeline "
        "refill path. CPSR/SPSR writes are denied until the native bridge can update "
        "CPU mode, banked registers, and the instruction pipeline safely."
    ),
    annotations=DESTRUCTIVE,
)
@_tool_errors
def cpu_register_write(
    core: Literal["arm9", "arm7"],
    name: RegisterName,
    value: U32Input,
    expected_session_id: SessionId,
    expected_stop_id: CounterInput,
    expected_state_version: CounterInput,
    verify: StrictBool = True,
) -> ToolEnvelope:
    return _result(
        backend.write_register(
            core,
            name=name,
            value=value,
            expected_session_id=expected_session_id,
            expected_stop_id=expected_stop_id,
            expected_state_version=expected_state_version,
            verify=verify,
        )
    )


@mcp.tool(
    name="memory_read",
    title="Read emulated memory",
    description=(
        "Read a bounded memory range through the active core. Only audited BIOS/TCM, "
        "RAM/WRAM, palette, VRAM, and OAM windows are allowed. MMIO, cartridge, and "
        "unaudited mappings are denied. Large output is bounded by configuration."
    ),
    annotations=READ_ONLY,
)
@_tool_errors
def memory_read(
    core: Literal["arm9", "arm7"],
    address: U32Input,
    length: MemoryLength,
    encoding: Literal["hex", "base64"] = "hex",
) -> ToolEnvelope:
    return _result(
        backend.read_memory(
            core,
            address=address,
            length=length,
            encoding=encoding,
        )
    )


@mcp.tool(
    name="memory_write",
    title="Write emulated memory",
    description=(
        "Write hex bytes to an audited memory window, verify, and attempt before-image "
        "rollback on failure. This is not externally atomic. Requires the complete "
        "expected snapshot. "
        "MMIO, cartridge, and unaudited mappings are always denied."
    ),
    annotations=DESTRUCTIVE,
)
@_tool_errors
def memory_write(
    core: Literal["arm9", "arm7"],
    address: U32Input,
    data_hex: HexPayload,
    expected_session_id: SessionId,
    expected_stop_id: CounterInput,
    expected_state_version: CounterInput,
    verify: StrictBool = True,
) -> ToolEnvelope:
    return _result(
        backend.write_memory(
            core,
            address=address,
            data_hex=data_hex,
            expected_session_id=expected_session_id,
            expected_stop_id=expected_stop_id,
            expected_state_version=expected_state_version,
            verify=verify,
        )
    )


@mcp.tool(
    name="breakpoint_set",
    title="Add or remove an execution breakpoint",
    description=(
        "Add or remove an upstream melonDS execution breakpoint on the active stopped "
        "core. Auto mode derives ARM/Thumb kind from CPSR. Requires the complete "
        "expected snapshot. ARM addresses must be word-aligned. "
        "Watchpoints are intentionally not exposed because upstream currently lacks "
        "the required bus-access hooks."
    ),
    annotations=DESTRUCTIVE,
)
@_tool_errors
def breakpoint_set(
    core: Literal["arm9", "arm7"],
    address: U32Input,
    enabled: StrictBool,
    expected_session_id: SessionId,
    expected_stop_id: CounterInput,
    expected_state_version: CounterInput,
    mode: Literal["auto", "arm", "thumb"] = "auto",
) -> ToolEnvelope:
    return _result(
        backend.set_breakpoint(
            core,
            address=address,
            enabled=enabled,
            mode=mode,
            expected_session_id=expected_session_id,
            expected_stop_id=expected_stop_id,
            expected_state_version=expected_state_version,
        )
    )


@mcp.tool(
    name="disassemble",
    title="Disassemble ARM or Thumb code",
    description=(
        "Read code from the active stopped core and decode up to 256 ARM/Thumb "
        "instructions with Capstone. Address defaults to the pipeline-corrected PC; "
        "auto mode uses the current CPSR T bit. Thumb decoding is constrained to "
        "ARMv4T/ARMv5TE Thumb-1 boundaries; ARM text is Capstone best-effort. "
        "MMIO-backed ranges are denied."
    ),
    annotations=READ_ONLY,
)
@_tool_errors
def disassemble(
    core: Literal["arm9", "arm7"],
    address: U32Input | None = None,
    count: InstructionCount = 16,
    mode: Literal["auto", "arm", "thumb"] = "auto",
) -> ToolEnvelope:
    return _result(
        backend.disassemble(core, address=address, count=count, mode=mode)
    )
