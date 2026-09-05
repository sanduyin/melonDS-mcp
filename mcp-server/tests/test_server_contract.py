from __future__ import annotations

import asyncio
from pathlib import Path
import sys

from mcp import Client, StdioServerParameters

from melonds_mcp.server import mcp


def test_mcp_tools_have_expected_schema_and_annotations() -> None:
    async def scenario() -> None:
        async with Client(mcp) as client:
            listing = await client.list_tools()
            tools = {tool.name: tool for tool in listing.tools}
            assert {
                "emulator_status",
                "emulator_attach",
                "emulator_detach",
                "emulator_activate_core",
                "emulator_resume",
                "cpu_step",
                "cpu_register_read",
                "cpu_register_write",
                "memory_read",
                "memory_write",
                "breakpoint_set",
                "disassemble",
            } == set(tools)
            assert "expected_stop_id" in tools["memory_write"].input_schema["required"]
            assert "expected_state_version" in tools["memory_write"].input_schema["required"]
            assert "expected_session_id" in tools["memory_write"].input_schema["required"]
            assert tools["emulator_status"].input_schema["additionalProperties"] is False
            assert tools["memory_read"].annotations.read_only_hint is True
            assert tools["memory_write"].annotations.destructive_hint is True
            assert tools["emulator_resume"].annotations.destructive_hint is True
            assert tools["cpu_step"].annotations.destructive_hint is True
            assert tools["memory_write"].annotations.open_world_hint is False

    asyncio.run(scenario())


def test_mcp_rejects_unknown_fields_and_lax_boolean_coercion() -> None:
    async def scenario() -> None:
        async with Client(mcp) as client:
            unknown = await client.call_tool("emulator_status", {"typo": 1})
            assert unknown.is_error is True
            assert "Extra inputs are not permitted" in unknown.content[0].text

            boolean_count = await client.call_tool(
                "cpu_step",
                {
                    "core": "arm9",
                    "expected_session_id": "not-attached",
                    "expected_stop_id": "0",
                    "expected_state_version": "0",
                    "count": True,
                },
            )
            assert boolean_count.is_error is True
            assert "valid integer" in boolean_count.content[0].text
            assert "SESSION_STATE" not in boolean_count.content[0].text

    asyncio.run(scenario())


def test_mcp_expected_failure_is_an_error_result() -> None:
    async def scenario() -> None:
        async with Client(mcp) as client:
            result = await client.call_tool("cpu_register_read", {"core": "arm9"})
            assert result.is_error is True
            assert "SESSION_STATE" in result.content[0].text

    asyncio.run(scenario())


def test_real_stdio_transport_stays_protocol_clean() -> None:
    async def scenario() -> None:
        project_dir = Path(__file__).resolve().parents[1]
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "melonds_mcp"],
            cwd=project_dir,
        )
        async with Client(parameters) as client:
            listing = await client.list_tools()
            assert any(tool.name == "emulator_status" for tool in listing.tools)
            result = await client.call_tool("emulator_status", {})
            assert result.is_error is False
            assert result.structured_content["snapshot"]["attached"] is False

    asyncio.run(scenario())
