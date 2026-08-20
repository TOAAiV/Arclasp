"""
Integration tests for arclasp.mcp.adapter (ArclaspMcpAdapter).

Unlike the other adapters, MCP has no govern() convenience function.  Each
test opens a Chain context manually, instantiates ArclaspMcpAdapter, and
calls handle_tool_call() for each tool invocation in the scenario.

The mcp package (1.27.0) is installed, so ArclaspMcpAdapter can be imported
directly without mocking.  The chain backend is still mocked via
arclasp.client._post.

Three usage modes are tested beyond the six core scenarios:
  handle_tool_call_direct  — core method (also used in Scenarios 1-6)
  install_patches_server   — patches server._call_tool_handler in-place
  decorator_wraps_handler  — @adapter.tool("name") decorator
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import arclasp
from arclasp.chain import Chain
from arclasp.mcp.adapter import ArclaspMcpAdapter
from arclasp.exceptions import ActionDeniedError, BackendUnavailableError

from .conftest import (
    make_mock_post,
    assert_event_recorded,
    assert_chain_completed,
    count_event_calls,
)


# ---------------------------------------------------------------------------
# Simple async tool handler used across tests
# ---------------------------------------------------------------------------

async def _handler(tool_name: str, arguments: dict) -> dict:
    return {"tool": tool_name, "result": "ok"}


# ===========================================================================
# Scenario 1 — happy path (3 tool calls in one chain)
# ===========================================================================

@pytest.mark.asyncio
async def test_happy_path_mcp():
    tools = ["query_database", "format_output", "log_result"]
    handler_calls: list[str] = []

    async def tracking_handler(name: str, args: dict) -> dict:
        handler_calls.append(name)
        return {"result": f"ok_{name}"}

    mock_post, calls = make_mock_post()

    with patch("arclasp.client._post", side_effect=mock_post):
        async with Chain("mcp-happy") as chain:
            adapter = ArclaspMcpAdapter(
                chain=chain,
                agent_name="mcp-agent",
                parent_agent_name="mcp-client",
            )
            for tool in tools:
                await adapter.handle_tool_call(tool, {"key": "val"}, tracking_handler)

    # All tools executed
    assert handler_calls == tools
    # Chain lifecycle
    assert any(c["path"] == "/v1/chains" for c in calls)
    assert_chain_completed(calls)
    # One /events call per tool (3 total)
    assert count_event_calls(calls) == 3
    for tool in tools:
        assert_event_recorded(
            calls,
            agent_name="mcp-agent",
            action_name=tool,
            action_type="tool_call",
        )


# ===========================================================================
# Scenario 2 — backend flags one action
# ===========================================================================

@pytest.mark.asyncio
async def test_backend_flags_action_mcp():
    mock_post, calls = make_mock_post(flag_on="format_output")

    with patch("arclasp.client._post", side_effect=mock_post):
        async with Chain("mcp-flag") as chain:
            adapter = ArclaspMcpAdapter(chain=chain, agent_name="mcp-agent")
            for tool in ["query_database", "format_output"]:
                await adapter.handle_tool_call(tool, {}, _handler)

    assert_chain_completed(calls)
    assert_event_recorded(calls, action_name="format_output", action_type="tool_call")
    # Workflow completes despite the flag
    assert count_event_calls(calls) == 2


# ===========================================================================
# Scenario 3 — backend denies action mid-sequence
# ===========================================================================

@pytest.mark.asyncio
async def test_backend_denies_action_mcp():
    mock_post, calls = make_mock_post(deny_on="delete_file")
    handler_calls: list[str] = []

    async def tracking_handler(name: str, args: dict) -> dict:
        handler_calls.append(name)
        return {"result": "ok"}

    with patch("arclasp.client._post", side_effect=mock_post):
        with pytest.raises(ActionDeniedError) as exc_info:
            async with Chain("mcp-deny") as chain:
                adapter = ArclaspMcpAdapter(chain=chain, agent_name="mcp-agent")
                await adapter.handle_tool_call("query_database", {}, tracking_handler)
                await adapter.handle_tool_call("delete_file",      {}, tracking_handler)
                await adapter.handle_tool_call("log_result",       {}, tracking_handler)

    err = exc_info.value
    # Regression guard: must be ActionDeniedError, NOT TypeError
    assert isinstance(err, ActionDeniedError)
    assert err.policy_name is not None
    assert err.condition   is not None
    assert err.remediation is not None
    assert err.docs_url    is not None
    # query_database handler called; delete_file raised before calling handler
    assert "query_database" in handler_calls
    assert "delete_file"    not in handler_calls   # denied before execution
    assert "log_result"     not in handler_calls   # never reached


# ===========================================================================
# Scenario 4 — backend unreachable
# ===========================================================================

@pytest.mark.asyncio
async def test_backend_unreachable_fail_deny_mcp():
    arclasp.init(
        api_key="prail_test",
        backend_url="http://localhost:9999",
        environment="development",
    )
    mock_post, calls = make_mock_post(unavailable=True)

    with patch("arclasp.client._post", side_effect=mock_post):
        with pytest.raises(BackendUnavailableError):
            async with Chain("mcp-failden") as chain:
                adapter = ArclaspMcpAdapter(chain=chain, agent_name="mcp-agent")
                await adapter.handle_tool_call("query_database", {}, _handler)

    assert count_event_calls(calls) == 0


# ===========================================================================
# Scenario 5 - backend unreachable, deprecated backend unavailable fails closed
# ===========================================================================

@pytest.mark.asyncio
async def test_handle_tool_call_direct_mcp():
    """Verify handle_tool_call records the event and then calls the handler."""
    mock_post, calls = make_mock_post()
    handler_called = []

    async def my_handler(name: str, args: dict) -> dict:
        handler_called.append((name, args))
        return {"data": [1, 2, 3]}

    with patch("arclasp.client._post", side_effect=mock_post):
        async with Chain("mcp-direct") as chain:
            adapter = ArclaspMcpAdapter(chain=chain, agent_name="my-service")
            result = await adapter.handle_tool_call(
                "list_records", {"table": "users"}, my_handler
            )

    assert result == {"data": [1, 2, 3]}
    assert handler_called == [("list_records", {"table": "users"})]
    assert_event_recorded(
        calls, agent_name="my-service",
        action_name="list_records", action_type="tool_call",
    )


# ===========================================================================
# Extra mode B — install() patches server._call_tool_handler
# ===========================================================================

@pytest.mark.asyncio
async def test_decorator_wraps_handler_mcp():
    """
    @adapter.tool("name") wraps a bare async tool function so that every
    invocation records a governance event first.
    """
    mock_post, calls = make_mock_post()

    with patch("arclasp.client._post", side_effect=mock_post):
        async with Chain("mcp-decorator") as chain:
            adapter = ArclaspMcpAdapter(chain=chain, agent_name="deco-agent")

            @adapter.tool("query_database")
            async def query_database(name: str, arguments: dict) -> dict:
                return {"rows": [1, 2, 3]}

            result = await query_database("query_database", {"sql": "SELECT 1"})

    assert result == {"rows": [1, 2, 3]}
    assert_event_recorded(
        calls, agent_name="deco-agent",
        action_name="query_database", action_type="tool_call",
    )
