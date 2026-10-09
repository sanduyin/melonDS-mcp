"""Regressions for observed view, clock-query, and slot-result mistakes."""

from types import SimpleNamespace

import pytest

from melonds_mcp import tools_control, tools_debug, tools_status
from melonds_mcp.emulator import EmulatorState


def registered(module, emu):
    functions = {}
    class Registry:
        def tool(self):
            def add(fn):
                functions[fn.__name__] = fn
                return fn
            return add
    module.register(Registry(), emu)
    return functions


@pytest.mark.parametrize("thumb,code,mnemonics", [
    (False, "0700a0e31eff2fe1", ["mov", "bx"]),
    (True, "4018704700000000", ["adds", "bx"]),
])
def test_disassembly_reads_instruction_backing_without_bus(thumb, code, mnemonics):
    calls = []
    def peek(cpu, address, length):
        calls.append((cpu, address, length))
        return bytes.fromhex(code)
    def forbidden(*args):
        pytest.fail("disassembly must never read the bus")
    emu = SimpleNamespace(lib=SimpleNamespace(lib=None, code_peek_block=peek, read_block=forbidden))
    tool = registered(tools_debug, emu)["disassemble"]
    result = tool(0x1000, count=2, cpu=0, thumb=thumb)
    assert calls == [(0, 0x1000, 8)]
    assert [entry["mnemonic"] for entry in result["instructions"]] == mnemonics
    assert result["access"] == "debug_peek_instruction_backing"
    assert result["byte_length"] == 8
    assert len(result["sha256"]) == 64


def test_disassembly_maximum_is_4096_bytes_and_rejects_unaligned_entry():
    calls = []
    def peek(cpu, address, length):
        calls.append(length)
        assert length == 4096
        return bytes.fromhex("0700a0e3") * 1024
    emu = SimpleNamespace(lib=SimpleNamespace(lib=None, code_peek_block=peek))
    tool = registered(tools_debug, emu)["disassemble"]
    assert len(tool(0x1000, count=1024, thumb=False)["instructions"]) == 1024
    with pytest.raises(ValueError, match="对齐"):
        tool(0x1002, thumb=False)
    assert calls == [4096]


def test_system_info_does_not_consume_clock_statistics():
    calls = []
    def cycles(mode):
        calls.append(mode)
        assert mode in (0, 2), "mode 1 changes the native baseline"
        return {0: 1234567, 2: 89}[mode]
    native = SimpleNamespace(melonds_get_cycles=cycles, melonds_jit_enabled=lambda: 0,
                             melonds_get_skip_render=lambda: 0)
    emu = SimpleNamespace(lib=SimpleNamespace(lib=native), ensure_init=lambda: None,
                          _jit_suppressed=False)
    tool = registered(tools_status, emu)["get_system_info"]
    first, second = tool(), tool()
    assert first == second
    assert first["system_clock_cycles"] == first["cycles_arm7"] == 1234567
    assert first["cycles_in_frame"] == 89
    assert calls == [0, 2, 0, 2]


def test_status_does_not_consume_clock_statistics():
    def cycles(mode):
        assert mode == 0, "status must not change the native cycle baseline"
        return 789
    native = SimpleNamespace(melonds_get_cycles=cycles,
                             melonds_debug_hooks_active=lambda: 0,
                             melonds_debug_data_hooks_active=lambda: 0,
                             melonds_debug_step_pending=lambda: 0,
                             melonds_debug_trace_active=lambda: 0)
    emu = SimpleNamespace(lib=SimpleNamespace(lib=native, get_status=lambda: [0] * 9,
                                              rom_info=lambda: None, break_info=lambda: {}),
                          ensure_init=lambda: None, fps=0, emulation_speed=0,
                          _rom_path=None, _watches={})
    assert EmulatorState.status_summary(emu)["system_clock_cycles"] == 789


@pytest.mark.parametrize("success", [0, 1])
def test_slot_calls_checked_api_and_preserves_its_result(tmp_path, success):
    calls = []
    rom = tmp_path / "游戏.nds"
    target = rom.with_suffix(".slot3.mst")
    target.write_bytes(b"fixture existence only")
    def checked(path):
        calls.append(path)
        return success
    native = SimpleNamespace(melonds_savestate_save=checked, melonds_savestate_load=checked)
    emu = SimpleNamespace(lib=SimpleNamespace(lib=native), rom_path=str(rom))
    tools = registered(tools_control, emu)
    for name in ("savestate_save", "savestate_load"):
        result = tools[name](slot=3)
        assert result["ok"] is bool(success)
        assert result["path"] == str(target)
        assert result["slot"] == 3
        assert ("error" in result) is (not success)
    assert calls == [str(target).encode("utf-8")] * 2


def test_slot_without_rom_fails_before_native_access():
    emu = SimpleNamespace(lib=SimpleNamespace(lib=None), rom_path=None)
    tools = registered(tools_control, emu)
    for name in ("savestate_save", "savestate_load"):
        with pytest.raises(ValueError, match="先加载 ROM"):
            tools[name](slot=1)
