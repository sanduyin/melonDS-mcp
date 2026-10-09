"""Atomic debug write/guard contracts; native execution is tested separately."""
import asyncio
import ctypes
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from melonds_mcp import tools_memory
from melonds_mcp.libmelonds import LibMelonDS
from melonds_mcp.tool_boundary import ToolBoundary


class Memory:
    def __init__(self):
        self.data = bytearray(b"\x11" * 32)
        self.code = bytearray(b"\x22" * 32)
        self.calls = []
        self.result = None
        self.refuse = False

    def peek(self, cpu, address, size, destination, view):
        self.calls.append(("peek", cpu, address, size, view))
        for index in range(size):
            destination[index] = (self.code if view else self.data)[index]
        return size

    def poke(self, cpu, address, size, source, view):
        self.calls.append(("poke", cpu, address, size, view))
        if not self.refuse:
            (self.code if view else self.data)[:size] = bytes(source)
        return size if self.result is None else self.result


def wrapper():
    memory = Memory()
    lib = object.__new__(LibMelonDS)
    lib.lib = SimpleNamespace(
        melonds_memory_poke_block=memory.poke,
        melonds_memory_peek_block=lambda *args: memory.peek(*args, 0),
        melonds_code_peek_block=lambda *args: memory.peek(*args, 1),
    )
    return lib, memory


@pytest.mark.parametrize("instruction,view", [(False, 0), (True, 1)])
def test_binding_forwards_exact_bytes_and_view(instruction, view):
    lib, memory = wrapper()
    assert lib.poke_block(1, 0x02000000, b"\xAA\xBB", instruction=instruction) == 2
    assert memory.calls == [("poke", 1, 0x02000000, 2, view)]
    assert bytes((memory.code if view else memory.data)[:2]) == b"\xAA\xBB"


@pytest.mark.parametrize("cpu,address,data,instruction", [
    (2, 0, b"x", False), (True, 0, b"x", False), (0, True, b"x", False),
    (0, -1, b"x", False), (0, 0xFFFFFFFF, b"xx", False),
    (0, 0, b"", False), (0, 0, b"x" * 4097, False), (0, 0, "xx", False),
    (0, 0, bytearray(b"x"), False), (0, 0, b"x", 1),
])
def test_binding_rejects_invalid_input_before_native(cpu, address, data, instruction):
    lib, memory = wrapper()
    with pytest.raises(ValueError):
        lib.poke_block(cpu, address, data, instruction=instruction)
    assert memory.calls == []


@pytest.mark.parametrize("count", [0, 1, 3])
def test_native_rejection_or_invalid_count_never_claims_success(count):
    lib, memory = wrapper()
    memory.result = count
    with pytest.raises(RuntimeError, match="invalid length"):
        lib.poke_block(0, 0x02000000, b"xx")


def test_missing_new_native_apis_do_not_fall_back():
    lib, memory = wrapper()
    del lib.lib.melonds_memory_poke_block
    del lib.lib.melonds_code_peek_block
    with pytest.raises(RuntimeError, match="lacks melonds_memory_poke_block"):
        lib.poke_block(0, 0x02000000, b"x")
    with pytest.raises(RuntimeError, match="lacks melonds_code_peek_block"):
        lib.code_peek_block(0, 0x02000000, 1)
    assert memory.calls == []


@pytest.fixture
def client():
    lib, memory = wrapper()
    lock = threading.RLock()
    def status():
        assert lock._is_owned()
        return [0, 19]
    lib.get_status = status
    emu = SimpleNamespace(lib=lib, lock=lock, ensure_init=lambda: None)
    server = FastMCP("write-test")
    tools_memory.register(ToolBoundary(server, emu), emu)
    def call(name, **args):
        return asyncio.run(server._tool_manager.get_tool(name).run(args))
    return call, memory


@pytest.mark.parametrize("name,expected,view", [("memory_poke", "1111", 0), ("code_patch", "2222", 1)])
def test_guard_readback_and_hashes_use_matching_view(client, name, expected, view):
    call, memory = client
    result = call(name, address=0x02000000, hex_data="AaBb", expected_hex=expected)
    assert result["previous_hex"] == expected and result["hex"] == "aabb"
    assert result["sha256"] == hashlib.sha256(b"\xAA\xBB").hexdigest()
    assert result["expected_bytes_checked"] is True
    assert result["prefetch_refresh_requested"] == bool(view)
    assert result["frame_number"] == 19
    assert [item[0] for item in memory.calls] == ["peek", "poke", "peek"]
    assert all(item[-1] == view for item in memory.calls)


@pytest.mark.parametrize("name", ["memory_poke", "code_patch"])
def test_stale_guard_never_writes(client, name):
    call, memory = client
    with pytest.raises(ToolError, match="EXPECTED_BYTES_MISMATCH"):
        call(name, address=0x02000000, hex_data="1234", expected_hex="0000")
    assert all(item[0] == "peek" for item in memory.calls)
    assert memory.data == bytearray(b"\x11" * 32) and memory.code == bytearray(b"\x22" * 32)


@pytest.mark.parametrize("args", [
    {"hex_data": "00", "expected_hex": "0000"},
    {"hex_data": "0"}, {"hex_data": "01 02"}, {"expected_hex": ""},
    {"expected_hex": False}, {"cpu": True}, {"address": 0xFFFFFFFF},
    {"extra": 1},
])
def test_tool_rejects_invalid_inputs_without_write(client, args):
    call, memory = client
    arguments = {"address": 0x02000000, "hex_data": "0000"}
    arguments.update(args)
    with pytest.raises(ToolError):
        call("memory_poke", **arguments)
    assert all(item[0] != "poke" for item in memory.calls)


def test_readback_failure_is_not_reported_as_success(client):
    call, memory = client
    memory.refuse = True
    with pytest.raises(ToolError, match="DEBUG_WRITE_VERIFICATION_FAILED"):
        call("memory_poke", address=0x02000000, hex_data="aabb")


def test_code_peek_does_not_read_data_overlay(client):
    call, memory = client
    result = call("code_peek", address=0x03000000, length=2)
    assert result["hex"] == "2222" and result["access"] == "debug_peek_instruction_backing"
    assert memory.calls == [("peek", 0, 0x03000000, 2, 1)]


def test_concurrent_stale_writes_only_one_commits(client):
    call, memory = client
    barrier = threading.Barrier(2)
    def write(value):
        barrier.wait()
        try:
            call("memory_poke", address=0x02000000, hex_data=value, expected_hex="1111")
            return True
        except ToolError as error:
            assert "EXPECTED_BYTES_MISMATCH" in str(error)
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, ["aaaa", "bbbb"]))
    assert sorted(results) == [False, True]
    assert sum(item[0] == "poke" for item in memory.calls) == 1
