"""
sdk/tests/test_adapter_input_validation.py
==========================================
Regression tests for adapter input validation hardening.

SDK-S-10: action_name is truncated to _ACTION_NAME_MAX (100) chars in
LangChain and LangGraph adapters, matching the existing CrewAI cap.

SDK-S-11: sanitize_log_field() in _utils.py escapes CR/LF characters in
framework-supplied strings before they reach logger calls.
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock
from uuid import uuid4

import pytest

import arclasp
from arclasp._constants import _ACTION_NAME_MAX
from arclasp._utils import sanitize_log_field


# ---------------------------------------------------------------------------
# Shared setup
# ---------------------------------------------------------------------------

def _init() -> None:
    arclasp.init(api_key="prail_test", backend_url="http://localhost:9999")


def _chain_start_response(chain_id: str = "test-chain-id") -> dict:
    return {"id": chain_id, "status": "active"}


def _allow_response() -> dict:
    return {
        "policy_decision": "allow",
        "decision_reason": "",
        "decision_source": "backend_evaluation",
    }


# ---------------------------------------------------------------------------
# SDK-S-10: action_name truncation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_langchain_tool_name_truncated_in_action_name():
    """
    A 150-char tool_name from LangChain must arrive at record_agent_action
    as action_name with length ≤ _ACTION_NAME_MAX (100).
    """
    from arclasp.langchain.callbacks import ArclaspLangChainCallback
    from arclasp.chain import Chain

    _init()
    long_name = "x" * 150

    recorded: list[dict] = []

    async def mock_post(path, body, action_type=None, headers=None):
        if path == "/v1/chains":
            return _chain_start_response()
        if path.endswith("/complete"):
            return {"status": "completed"}
        recorded.append(body)
        return _allow_response()

    with patch("arclasp.client._post", side_effect=mock_post):
        async with Chain("test") as chain:
            cb = ArclaspLangChainCallback(chain)
            run_id = uuid4()
            await cb.on_tool_start(
                {"name": long_name},
                "some_input",
                run_id=run_id,
            )

    assert recorded, "No event body recorded"
    action_name = recorded[0]["action_name"]
    assert len(action_name) <= _ACTION_NAME_MAX, (
        f"action_name length {len(action_name)} exceeds _ACTION_NAME_MAX={_ACTION_NAME_MAX}"
    )


@pytest.mark.asyncio
async def test_langchain_model_name_truncated_in_action_name():
    """
    A 150-char model_name from LangChain must arrive at record_agent_action
    as action_name with length ≤ _ACTION_NAME_MAX (100).
    """
    from arclasp.langchain.callbacks import ArclaspLangChainCallback
    from arclasp.chain import Chain

    _init()
    long_name = "m" * 150

    recorded: list[dict] = []

    async def mock_post(path, body, action_type=None, headers=None):
        if path == "/v1/chains":
            return _chain_start_response()
        if path.endswith("/complete"):
            return {"status": "completed"}
        recorded.append(body)
        return _allow_response()

    with patch("arclasp.client._post", side_effect=mock_post):
        async with Chain("test") as chain:
            cb = ArclaspLangChainCallback(chain)
            run_id = uuid4()
            await cb.on_llm_start(
                {"name": long_name},
                ["prompt text"],
                run_id=run_id,
            )

    assert recorded, "No event body recorded"
    action_name = recorded[0]["action_name"]
    assert len(action_name) <= _ACTION_NAME_MAX, (
        f"action_name length {len(action_name)} exceeds _ACTION_NAME_MAX={_ACTION_NAME_MAX}"
    )


@pytest.mark.asyncio
async def test_langgraph_node_name_truncated_in_action_name():
    """
    A 150-char langgraph_node metadata value must arrive at record_agent_action
    as action_name with length ≤ _ACTION_NAME_MAX (100).
    """
    from arclasp.langgraph.callbacks import ArclaspLangGraphCallback
    from arclasp.chain import Chain

    _init()
    long_name = "n" * 150

    recorded: list[dict] = []

    async def mock_post(path, body, action_type=None, headers=None):
        if path == "/v1/chains":
            return _chain_start_response()
        if path.endswith("/complete"):
            return {"status": "completed"}
        recorded.append(body)
        return _allow_response()

    with patch("arclasp.client._post", side_effect=mock_post):
        async with Chain("test") as chain:
            cb = ArclaspLangGraphCallback(chain)
            await cb.on_node_start(
                long_name[:_ACTION_NAME_MAX - 7],  # simulate post-truncation (truncation happens in closure)
                {},
            )

    # Also verify via the LangChain bridge closure path — simulate on_chain_start
    # calling the closure with a long node_name in metadata.
    recorded_bridge: list[dict] = []

    async def mock_post2(path, body, action_type=None, headers=None):
        if path == "/v1/chains":
            return _chain_start_response()
        if path.endswith("/complete"):
            return {"status": "completed"}
        recorded_bridge.append(body)
        return _allow_response()

    with patch("arclasp.client._post", side_effect=mock_post2):
        async with Chain("test") as chain:
            from arclasp.langgraph.callbacks import _make_on_chain_start

            # Build a minimal handler object that mimics _AsLangChainCallback internals
            handler = MagicMock()
            handler._arclasp = ArclaspLangGraphCallback(chain)
            handler._node_runs = {}
            handler._all_nodes = {}
            handler._node_parents = {}

            start_fn = _make_on_chain_start()
            await start_fn(
                handler,
                serialized={},
                inputs={},
                run_id=uuid4(),
                parent_run_id=None,
                metadata={"langgraph_node": long_name},
            )

    assert recorded_bridge, "No event body recorded via bridge"
    action_name = recorded_bridge[0]["action_name"]
    assert len(action_name) <= _ACTION_NAME_MAX, (
        f"action_name length {len(action_name)} exceeds _ACTION_NAME_MAX={_ACTION_NAME_MAX}"
    )


# ---------------------------------------------------------------------------
# SDK-S-11: sanitize_log_field helper
# ---------------------------------------------------------------------------

def test_sanitize_log_field_escapes_lf():
    """LF inside a framework-supplied name must be rendered as literal \\n."""
    assert sanitize_log_field("node\ninjection") == "node\\ninjection"


def test_sanitize_log_field_escapes_cr():
    """CR inside a framework-supplied name must be rendered as literal \\r."""
    assert sanitize_log_field("node\rinjection") == "node\\rinjection"


def test_sanitize_log_field_passthrough():
    """A clean name with no CR/LF must pass through unchanged."""
    assert sanitize_log_field("clean_node_name") == "clean_node_name"
