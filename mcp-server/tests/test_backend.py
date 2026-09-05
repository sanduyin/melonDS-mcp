from __future__ import annotations

import pytest

from melonds_mcp.backend import MelonDSBackend
from melonds_mcp.errors import SessionError, ValidationError

from .fake_rsp import FakeRspServer


def _guard(status: dict[str, object]) -> dict[str, object]:
    return {
        "expected_session_id": status["session_id"],
        "expected_stop_id": status["stop_id"],
        "expected_state_version": status["state_version"],
    }


def test_dual_core_switch_is_explicit_and_stop_guarded() -> None:
    with FakeRspServer() as arm9, FakeRspServer() as arm7:
        arm9.registers[15] = 0x02000000
        arm7.registers[15] = 0x03800000
        backend = MelonDSBackend()
        attached = backend.attach(
            arm9_port=arm9.port,
            arm7_port=arm7.port,
            active_core="arm9",
        )
        assert attached["active_core"] == "arm9"
        assert attached["stop_id"] == "1"

        with pytest.raises(SessionError, match="emulator_activate_core"):
            backend.read_registers("arm7")

        switched = backend.activate_core("arm7", **_guard(attached))
        assert switched["status"]["active_core"] == "arm7"
        assert switched["status"]["stop_id"] == "2"
        assert arm9.commands[-1] == "c"
        assert "<interrupt>" in arm7.commands

        with pytest.raises(SessionError, match="stale stop"):
            backend.write_register(
                "arm7",
                name="r0",
                value="0x1234",
                expected_session_id=attached["session_id"],
                expected_stop_id="1",
                expected_state_version=switched["status"]["state_version"],
            )
        current = switched["status"]
        result = backend.write_register(
            "arm7",
            name="r0",
            value="0x1234",
            **_guard(current),
        )
        assert result["verified"] is True
        assert result["actual"] == "0x00001234"
        with pytest.raises(ValidationError, match="CPSR/SPSR writes are unavailable"):
            backend.write_register(
                "arm7",
                name="cpsr",
                value="0x20",
                **_guard(backend.status()),
            )
        with pytest.raises(ValidationError, match="CPSR/SPSR writes are unavailable"):
            backend.write_register(
                "arm7",
                name="spsr_irq",
                value="0x13",
                **_guard(backend.status()),
            )
        breakpoint_status = backend.status()
        backend.set_breakpoint(
            "arm7",
            address="0x03800010",
            enabled=True,
            **_guard(breakpoint_status),
        )
        detached = backend.detach(**_guard(backend.status()))
        assert detached["removed_breakpoints"] == 1
        assert any(command.startswith("z1,3800010,4") for command in arm7.commands)


def test_mmio_is_denied_by_default() -> None:
    with FakeRspServer() as arm9:
        backend = MelonDSBackend()
        backend.attach(
            arm9_port=arm9.port,
            cores="arm9",
            active_core="arm9",
        )
        status = backend.status()
        with pytest.raises(ValidationError, match="side-effect-free allowlist"):
            backend.read_memory("arm9", address="0x04000000", length=4)
        with pytest.raises(ValidationError, match="side-effect-free allowlist"):
            backend.write_memory(
                "arm9",
                address="0x04000000",
                data_hex="00000000",
                **_guard(status),
            )
        with pytest.raises(ValidationError, match="side-effect-free allowlist"):
            backend.disassemble("arm9", address="0x04000000", count=1)
        with pytest.raises(ValidationError, match="cartridge"):
            backend.read_memory("arm9", address="0x0a000000", length=1)
        backend.detach(**_guard(backend.status()))


def test_memory_write_roundtrip_and_arm_disassembly() -> None:
    with FakeRspServer() as arm9:
        arm9.registers[15] = 0x02000000
        arm9.registers[16] = 0
        arm9.set_memory(0x02000000, bytes.fromhex("0000a0e11eff2fe1"))
        backend = MelonDSBackend()
        backend.attach(
            arm9_port=arm9.port,
            cores="arm9",
            active_core="arm9",
        )
        status = backend.status()
        write = backend.write_memory(
            "arm9",
            address="0x02000008",
            data_hex="de ad be ef",
            **_guard(status),
        )
        assert write == {
            "core": "arm9",
            "address": "0x02000008",
            "bytes_written": 4,
            "atomic": False,
            "rollback_strategy": "best_effort_before_image",
            "verified": True,
        }
        read = backend.read_memory("arm9", address="0x02000008", length=4)
        assert read["data"] == "deadbeef"

        decoded = backend.disassemble("arm9", count=2)
        assert decoded["mode"] == "arm"
        assert decoded["decoded_count"] == 2
        assert decoded["instructions"][0]["mnemonic"] == "mov"
        backend.detach(**_guard(backend.status()))
