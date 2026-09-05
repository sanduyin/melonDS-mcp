"""Safe peek binding and MCP interface; no native execution required."""

import asyncio
import ctypes
import hashlib
import threading
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from melonds_mcp.libmelonds import LibMelonDS
from melonds_mcp.tool_boundary import ToolBoundary
from melonds_mcp import tools_memory


class NativeFunction:
    def __init__(self, result=None):
        self.result = result
        self.calls = []

    def __call__(self, cpu, address, length, destination):
        self.calls.append((cpu, address, length))
        for i in range(length):
            destination[i] = (address + i) & 255
        return length if self.result is None else self.result


def wrapper(peek=True, result=None):
    native = SimpleNamespace()
    if peek:
        native.melonds_memory_peek_block = NativeFunction(result)
    lib = object.__new__(LibMelonDS)
    lib.lib = native
    return lib


def test_peek_block_returns_exact_native_bytes():
    lib = wrapper()
    assert lib.peek_block(1, 0x038000FE, 4) == b"\xfe\xff\x00\x01"
    assert lib.lib.melonds_memory_peek_block.calls == [(1, 0x038000FE, 4)]


@pytest.mark.parametrize("cpu,address,size", [
    (-1, 0, 1), (2, 0, 1), (True, 0, 1), ("0", 0, 1),
    (0, -1, 1), (0, 0x100000000, 1), (0, True, 1), (0, "0", 1),
    (0, 0, 0), (0, 0, -1), (0, 0, 4097), (0, 0, True), (0, 0, "4"),
    (0, 0xFFFFFFFF, 2),
])
def test_python_bounds_fail_before_native_call(cpu, address, size):
    lib = wrapper()
    with pytest.raises(ValueError):
        lib.peek_block(cpu, address, size)
    assert lib.lib.melonds_memory_peek_block.calls == []


@pytest.mark.parametrize("copied", [0, 3, 5])
def test_rejected_short_or_overlong_native_copy_never_returns_padding(copied):
    lib = wrapper(result=copied)
    with pytest.raises(RuntimeError, match="debug peek refused or returned a short read"):
        lib.peek_block(0, 0x02000000, 4)


def test_missing_api_does_not_fall_back_to_bus_read():
    lib = wrapper(peek=False)
    def forbidden_bus_read(*args):
        raise AssertionError("must not use the bus API")
    lib.lib.melonds_memory_read_block = forbidden_bus_read
    with pytest.raises(RuntimeError, match="lacks melonds_memory_peek_block"):
        lib.peek_block(0, 0x02000000, 4)


class DeclarationLibrary:
    """Callable placeholders for all old declarations, optionally no new API."""
    def __init__(self, has_peek):
        self.has_peek = has_peek
        self.functions = {}

    def __getattr__(self, name):
        if name == "melonds_memory_peek_block" and not self.has_peek:
            raise AttributeError(name)
        return self.functions.setdefault(name, SimpleNamespace())


@pytest.mark.parametrize("has_peek", [False, True])
def test_optional_declaration_preserves_older_library_loading(has_peek):
    lib = object.__new__(LibMelonDS)
    lib.lib = DeclarationLibrary(has_peek)
    lib._declare()
    if has_peek:
        function = lib.lib.melonds_memory_peek_block
        assert function.argtypes == [ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32,
                                     ctypes.POINTER(ctypes.c_ubyte)]
        assert function.restype is ctypes.c_uint32
    else:
        assert "melonds_memory_peek_block" not in lib.lib.functions


@pytest.fixture
def server_and_lib():
    lib = wrapper()
    lock = threading.RLock()
    status_calls = []
    def status():
        assert lock._is_owned()
        status_calls.append(1)
        return [1, 789] + [0] * 7
    lib.get_status = status
    emu = SimpleNamespace(lib=lib, lock=lock, ensure_init=lambda: None)
    server = FastMCP("peek-test")
    tools_memory.register(ToolBoundary(server, emu), emu)
    return server, lib, status_calls


def invoke(server, args):
    return asyncio.run(server._tool_manager.get_tool("memory_peek").run(args))


def test_tool_preserves_hex_hash_access_cpu_address_and_locked_frame(server_and_lib):
    server, lib, status_calls = server_and_lib
    result = invoke(server, {"address": 0x02000000, "length": 4, "cpu": 0})
    assert result["hex"] == "00010203"
    assert result["sha256"] == hashlib.sha256(bytes(range(4))).hexdigest()
    assert result["access"] == "debug_peek_data_view"
    assert (result["cpu"], result["address"], result["length"], result["frame_number"]) == (0, 0x02000000, 4, 789)
    assert "not the instruction cache" in result["notes"][0]
    assert status_calls == [1]
    assert lib.lib.melonds_memory_peek_block.calls == [(0, 0x02000000, 4)]


def test_tool_default_and_schema(server_and_lib):
    server, lib, _ = server_and_lib
    result = invoke(server, {"address": 0x02000000})
    assert result["length"] == 256
    tool = next(tool for tool in asyncio.run(server.list_tools()) if tool.name == "memory_peek")
    assert tool.name == "memory_peek"
    assert tool.inputSchema["additionalProperties"] is False
    assert tool.inputSchema["properties"]["length"]["maximum"] == 4096


@pytest.mark.parametrize("args", [
    {"address": -1}, {"address": "0x02000000"}, {"address": True},
    {"address": 0xFFFFFFFF, "length": 2}, {"address": 0, "length": 4097},
    {"address": 0, "length": False}, {"address": 0, "cpu": 2},
    {"address": 0, "cpu": True}, {"address": 0, "typo": 1},
])
def test_tool_rejects_invalid_arguments_without_native_access(server_and_lib, args):
    server, lib, status_calls = server_and_lib
    with pytest.raises(ToolError):
        invoke(server, args)
    assert lib.lib.melonds_memory_peek_block.calls == []
    assert status_calls == []


def test_tool_native_failure_is_mcp_error_without_status_read(server_and_lib):
    server, lib, status_calls = server_and_lib
    lib.lib.melonds_memory_peek_block.result = 0
    with pytest.raises(ToolError, match="debug peek refused"):
        invoke(server, {"address": 0x04000000, "length": 4})
    assert status_calls == []
