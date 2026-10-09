"""One serialized, bounded entry point for all MCP tools.

The native shim is a process-wide singleton. Python's GIL is not sufficient:
ctypes releases it while core code runs. Serialize complete tool transactions.
"""

from functools import wraps
import inspect
from typing import Annotated, Literal, get_origin, get_type_hints

from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.utilities.func_metadata import FuncMetadata
from pydantic import Field, StrictBool, StrictInt, StrictStr


class StrictMetadata(FuncMetadata):
    def pre_parse_json(self, data: dict) -> dict:
        # Tool arguments are already JSON; strings must not become lists/bools.
        return data


def _annotation(tool: str, name: str, original):
    # New tools carry their own strict bounds/enums. Do not replace e.g. the
    # palette's kind=(bg,obj) with the watchpoint's legacy kind=(r,w,rw).
    if get_origin(original) in (Annotated, Literal):
        return original
    if name == "cpu":
        choices = (-1, 0, 1) if tool.endswith(("_list", "_clear")) else (0, 1)
        return Annotated[StrictInt, Field(ge=min(choices), le=max(choices))]
    bounds = {
        "address": (0, 0xFFFFFFFF), "address_start": (0, 0xFFFFFFFF),
        "address_end": (0, 0xFFFFFFFF), "value": (0, 0xFFFFFFFF),
        "base_address": (0, 0xFFFFFFFF), "entry_address": (0, 0xFFFFFFFF),
        "timeout_seconds": (1, 120),
        "expected": (0, 0xFFFFFFFF), "frames": (1, 3600),
        "max_frames": (1, 3600), "x": (0, 255), "y": (0, 191),
        "length": (1, 4096), "size": (1, 4096),
        "count": (1, 1024 if tool == "disassemble" else 100000),
        "max_entries": (1, 65536), "cpu_mask": (1, 3),
        "bp_id": (0, 0x7FFFFFFF), "wp_id": (0, 0x7FFFFFFF),
        "watch_id": (1, 0x7FFFFFFF), "slot": (-1, 9),
    }
    if name in bounds:
        lo, hi = bounds[name]
        return Annotated[StrictInt, Field(ge=lo, le=hi)]
    if name == "hex_data":
        return Annotated[StrictStr, Field(min_length=2, max_length=8192,
                                         pattern=r"^(?:[0-9a-fA-F]{2})+$")]
    if name == "buttons":
        return Annotated[list[StrictStr], Field(max_length=12)]
    enums = {
        "screen": ("top", "bottom", "both"), "format": ("png", "rgb_hex"),
        "kind": ("r", "w", "rw"),
        "fmt": ("hex", "u8", "u16", "u32", "s8", "s16", "s32", "float", "ascii"),
        "wtype": ("u8", "u16", "u32", "s8", "s16", "s32", "float", "ascii"),
    }
    if name in enums:
        return Literal[enums[name]]
    if original is bool:
        return StrictBool
    if original == bool | None:
        return StrictBool | None
    if original is int:
        return StrictInt
    if original is str:
        return Annotated[StrictStr, Field(max_length=4096)]
    return original


def _cross_validate(tool: str, args: dict) -> None:
    if tool in ("write_memory", "advance_frames_until"):
        size = args.get("size", 4)
        if size not in (1, 2, 4):
            raise ValueError("size 必须为 1/2/4")
        value = args.get("value", args.get("expected", 0))
        if value >= 1 << (8 * size):
            raise ValueError("value/expected 超出指定宽度")
    if tool in ("savestate_save", "savestate_load"):
        path, slot = args.get("path", ""), args.get("slot", -1)
        if bool(path) == (slot in range(1, 10)) or slot == 0:
            raise ValueError("必须且只能指定 path 或 slot(1-9)")
    if tool == "trace_start" and args["address_start"] > args["address_end"]:
        raise ValueError("address_start 不得大于 address_end")
    if "address" in args:
        size = args.get("length", args.get("size", 1))
        if tool == "write_memory_bytes":
            size = len(args["hex_data"]) // 2
        elif tool == "disassemble":
            size = args.get("count", 10) * 4
        if args["address"] + size > 0x100000000:
            raise ValueError("内存范围超过 32 位地址空间")


class ToolBoundary:
    def __init__(self, server, emulator):
        self.server = server
        self.emulator = emulator

    def tool(self):
        def register(fn):
            signature = inspect.signature(fn)
            hints = get_type_hints(fn, include_extras=True)
            typed = signature.replace(parameters=[
                p.replace(annotation=_annotation(fn.__name__, p.name, hints[p.name]))
                for p in signature.parameters.values()
            ], return_annotation=hints.get("return", inspect.Signature.empty))

            @wraps(fn)
            def invoke(*args, **kwargs):
                bound = signature.bind(*args, **kwargs)
                bound.apply_defaults()
                try:
                    _cross_validate(fn.__name__, bound.arguments)
                    with self.emulator.lock:
                        self.emulator.ensure_init()
                        return fn(*args, **kwargs)
                except (ValueError, RuntimeError, OSError) as exc:
                    raise ToolError(str(exc)) from exc

            invoke.__signature__ = typed
            invoke.__annotations__ = {p.name: p.annotation for p in typed.parameters.values()}
            invoke.__annotations__["return"] = typed.return_annotation
            self.server.add_tool(invoke)
            # MCP 1.x builds a Pydantic model for each tool. Forbid silent extra
            # arguments, in addition to the public strict/bounded annotations.
            registered = self.server._tool_manager.get_tool(fn.__name__)
            model = registered.fn_metadata.arg_model
            model.model_config.update(extra="forbid", strict=True)
            model.model_rebuild(force=True)
            registered.parameters = model.model_json_schema()
            registered.fn_metadata = StrictMetadata(**registered.fn_metadata.model_dump())
            return invoke
        return register
