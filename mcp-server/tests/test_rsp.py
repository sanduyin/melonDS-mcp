from __future__ import annotations

import pytest

from melonds_mcp.errors import RspProtocolError
from melonds_mcp.rsp import RspClient, TargetState, parse_stop_reply

from .fake_rsp import FakeRspServer


def test_connect_halt_register_memory_step_and_breakpoint() -> None:
    with FakeRspServer() as fake:
        fake.registers[0] = 0x12345678
        fake.registers[15] = 0x02000000
        fake.set_memory(0x02000000, bytes.fromhex("0000a0e1"))
        client = RspClient("127.0.0.1", fake.port, core="arm9")
        client.connect(halt_on_connect=True)

        assert client.state == TargetState.STOPPED
        assert client.capabilities["hwbreak"] is True
        assert client.read_registers()["r0"] == 0x12345678
        assert client.read_memory(0x02000000, 4) == bytes.fromhex("0000a0e1")

        client.write_register("r1", 0xAABBCCDD)
        assert client.read_register("r1") == 0xAABBCCDD
        client.write_memory(0x02000002, b"\x11\x22\x33")
        assert fake.get_memory(0x02000002, 3) == b"\x11\x22\x33"
        client.set_breakpoint(0x02000010, kind=4, enabled=True)

        stop = client.step()
        assert stop.signal == 5
        assert client.read_register("pc") == 0x02000004
        client.close()


def test_connect_releases_then_interrupts_running_core() -> None:
    with FakeRspServer() as fake:
        client = RspClient("127.0.0.1", fake.port, core="arm7")
        client.connect(halt_on_connect=False)
        assert client.state == TargetState.RUNNING
        stop = client.pause()
        assert "c" in fake.commands
        assert stop.reason == "interrupt"
        assert client.state == TargetState.STOPPED
        client.close()


def test_pause_drains_coalesced_breakpoint_and_interrupt_stops() -> None:
    with FakeRspServer(
        coalesced_stop_on_interrupt=True,
        coalesced_stop_delay=0.1,
    ) as fake:
        fake.registers[0] = 0x11223344
        client = RspClient("127.0.0.1", fake.port, core="arm9")
        client.connect(halt_on_connect=False)

        stop = client.pause()
        assert stop.reason == "trap"
        assert client.stop_sequence == 1
        # A register command proves that the redundant S02 packet did not leak
        # into the next request/response exchange.
        assert client.read_register("r0") == 0x11223344
        client.close()


def test_bad_response_checksum_is_rejected() -> None:
    with FakeRspServer(bad_checksum_for="g") as fake:
        client = RspClient("127.0.0.1", fake.port, core="arm9")
        client.connect(halt_on_connect=True)
        with pytest.raises(RspProtocolError, match="checksum mismatch"):
            client.read_registers()
        assert client.state == TargetState.DISCONNECTED
        client.close()


def test_no_ack_disconnect_can_reconnect_cleanly() -> None:
    with FakeRspServer() as fake:
        first = RspClient("127.0.0.1", fake.port, core="arm9")
        first.connect(halt_on_connect=False)
        assert first.capabilities["QStartNoAckMode"] is True
        first.close()

        second = RspClient("127.0.0.1", fake.port, core="arm9")
        second.connect(halt_on_connect=True)
        assert second.state == TargetState.STOPPED
        second.close()


def test_parse_extended_stop_reply() -> None:
    reply = parse_stop_reply("T05swbreak:02001234;thread:1;")
    assert reply.kind == "stopped"
    assert reply.reason == "trap"
    assert reply.details == {"swbreak": "02001234", "thread": "1"}
