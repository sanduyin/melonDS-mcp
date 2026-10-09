import asyncio
import base64
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Annotated, Literal

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult
from pydantic import Field, StrictInt

from melonds_mcp.emulator import EmulatorState
from melonds_mcp.server import create_server
from melonds_mcp.tool_boundary import ToolBoundary


class Native:
    def __init__(self):
        self.frames = 0
        self.running = True
        self.jit = True
        self.step_pending = False
        self.hit = False
        self.calls = []
        self.buttons = 0

    def melonds_init(self):
        return 0

    def melonds_running(self):
        return self.running

    def melonds_set_skip_render(self, skip):
        self.calls.append(("render_skip", skip))

    def melonds_debug_hooks_active(self):
        return self.step_pending

    def melonds_debug_data_hooks_active(self):
        return False

    def melonds_jit_enabled(self):
        return self.jit

    def melonds_set_jit(self, enabled):
        self.calls.append(("jit", enabled))
        self.jit = bool(enabled)
        return 1

    def melonds_debug_step_request(self, cpu, count):
        self.calls.append(("step", cpu, count))
        self.step_pending = True

    def melonds_debug_step_pending(self):
        return self.step_pending

    def melonds_cycle(self):
        self.calls.append(("cycle",))
        if not self.running:
            return 0
        if self.step_pending:
            assert not self.jit
            self.step_pending = False
            self.hit = True
            return 1
        self.frames += 1
        return 0

    def melonds_input_keypad_get(self):
        return self.buttons

    def melonds_input_keypad_update(self, mask):
        self.buttons = mask


@pytest.fixture
def emu(monkeypatch):
    native = Native()
    wrapper = SimpleNamespace(
        lib=native,
        get_status=lambda: [native.running, native.frames] + [0] * 7,
        break_info=lambda: {"hit": native.hit},
        screenshot_bytes=lambda: b"\x12\x34\x56" * (256 * 384),
    )
    monkeypatch.setattr("melonds_mcp.emulator.LibMelonDS", lambda: wrapper)
    return EmulatorState()


def call(server, name, arguments):
    return asyncio.run(server._tool_manager.get_tool(name).run(arguments))


def test_all_tools_registered_and_extra_forbidden(emu):
    server, _ = create_server(emu)
    tools = asyncio.run(server.list_tools())
    assert len(tools) == 62
    assert all(tool.inputSchema["additionalProperties"] is False for tool in tools)
    assert next(t for t in tools if t.name == "advance_frames").inputSchema["properties"]["frames"]["maximum"] == 3600
    analysis_schema = next(t for t in tools if t.name == "decompile_bytes").inputSchema["properties"]
    assert analysis_schema["base_address"]["maximum"] == 0xFFFFFFFF
    assert analysis_schema["timeout_seconds"]["minimum"] == 1
    assert analysis_schema["timeout_seconds"]["maximum"] == 120


@pytest.mark.parametrize("arguments,valid", [
    ({"kind": "bg", "offset": 7}, True),
    ({"kind": "obj", "offset": 127}, True),
    ({"kind": "rw"}, False),
    ({"offset": -1}, False),
    ({"offset": 128}, False),
    ({"offset": "7"}, False),
])
def test_explicit_tool_annotations_survive_legacy_name_rules(emu, arguments, valid):
    server, _ = create_server(emu)

    @ToolBoundary(server, emu).tool()
    def palette_probe(
        kind: Literal["bg", "obj"] = "bg",
        offset: Annotated[StrictInt, Field(ge=0, le=127)] = 0,
    ) -> dict:
        return {"ok": True, "kind": kind, "offset": offset}

    if valid:
        call(server, "palette_probe", arguments)
    else:
        with pytest.raises((ToolError, ValueError)):
            call(server, "palette_probe", arguments)


@pytest.mark.parametrize("name,args", [
    ("advance_frames", {"frames": True}),
    ("advance_frames", {"frames": "2"}),
    ("advance_frames", {"frames": 0}),
    ("advance_frames", {"frames": 3601}),
    ("advance_frames", {"typo": 1}),
    ("set_buttons", {"buttons": '["a"]'}),
    ("get_pc", {"cpu": 2}),
    ("write_memory", {"address": -1, "value": 0}),
    ("write_memory", {"address": 0xFFFFFFFF, "value": 0}),
    ("write_memory", {"address": 0, "value": 256, "size": 1}),
    ("write_memory_bytes", {"address": 0, "hex_data": "zz"}),
    ("disassemble", {"address": 0, "count": -1}),
    ("trace_get", {"max_entries": -1}),
    ("savestate_save", {"path": "x", "slot": 1}),
    ("savestate_save", {"slot": 0}),
    ("screenshot", {"screen": "invalid"}),
])
def test_invalid_arguments_do_not_reach_native(emu, name, args):
    server, _ = create_server(emu)
    with pytest.raises(ToolError):
        call(server, name, args)
    assert emu.lib.lib.calls == []


def test_step_disables_jit_before_first_instruction_and_counts_no_partial_frame(emu):
    result = emu.step(0, 1)
    calls = emu.lib.lib.calls
    assert calls.index(("step", 0, 1)) < calls.index(("jit", 0)) < calls.index(("cycle",))
    assert result["hit"] is True
    assert result["frames_executed"] == 0


def test_paused_frames_are_not_counted(emu):
    emu.lib.lib.running = False
    result = emu.advance_frames(100)
    assert result["frames_executed"] == 0
    assert emu.lib.lib.calls.count(("cycle",)) == 1


def test_default_frames_are_rendered(emu):
    result = emu.advance_frames(3)
    assert result["frames_executed"] == 3
    assert result["frame_number"] == 3
    assert ("render_skip", 1) not in emu.lib.lib.calls


def test_screenshot_is_protocol_image_with_small_metadata(emu):
    server, _ = create_server(emu)
    result = asyncio.run(server.call_tool("screenshot", {"screen": "top"}))
    assert isinstance(result, CallToolResult)
    assert [c.type for c in result.content] == ["text", "image"]
    assert result.content[1].mimeType == "image/png"
    assert base64.b64decode(result.content[1].data).startswith(b"\x89PNG")
    assert "data_base64" not in result.structuredContent
    assert result.structuredContent["size"] == {"width": 256, "height": 192}


def test_key_names_match_hardware_bits(emu):
    emu.lib.lib.buttons = 1 << 3
    server, _ = create_server(emu)
    assert call(server, "get_buttons", {})["pressed"] == ["start"]


def test_complete_tools_are_serialized_even_without_gil(emu):
    server, _ = create_server(emu)
    native = emu.lib.lib
    active = 0
    maximum = 0
    guard = threading.Lock()
    def slow_read():
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.02)
        with guard:
            active -= 1
        return 0
    native.melonds_input_keypad_get = slow_read
    fn = server._tool_manager.get_tool("get_buttons").fn
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: fn(), range(8)))
    assert maximum == 1


def test_windows_library_candidates(monkeypatch):
    from melonds_mcp import libmelonds
    monkeypatch.setattr(libmelonds.sys, "platform", "win32")
    monkeypatch.delenv("MELONDS_MCP_LIB", raising=False)
    candidates = libmelonds._candidate_paths()
    assert any(str(p).endswith("build\\mcp\\Release\\melonds_mcp.dll") for p in candidates)
    assert all(p.suffix == ".dll" for p in candidates)
