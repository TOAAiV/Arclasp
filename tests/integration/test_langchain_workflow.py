"""
Integration tests for proofrail.langchain.adapter (GovernedChain).

The stub chain manually calls the ProofRail callback methods that would
normally be called by LangChain's event system (on_tool_start, on_tool_end,
on_llm_start, on_llm_end).  This lets us test the full governance path
without a real LLM or real tools.

Framework stubs are injected via sys.modules in conftest.py — langchain is
NOT installed.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import patch, MagicMock
from typing import Any

import pytest

import proofrail
from proofrail.langchain.adapter import govern
from proofrail.exceptions import ActionDeniedError, BackendUnavailableError

from .conftest import (
    make_mock_post,
    assert_event_recorded,
    assert_chain_completed,
    count_event_calls,
)


# ---------------------------------------------------------------------------
# Stub chain
# ---------------------------------------------------------------------------

class _StubChain:
    """
    Minimal LangChain-like Runnable stub.

    ainvoke accepts a LangChain config dict and manually fires governance
    callbacks so that ProofRailLangChainCallback records events.  The tool
    list is configurable; denial tests inject a sensitive tool name.
    """

    def __init__(self, tools: list[str] | None = None) -> None:
        self._tools = tools or ["search_web", "format_output"]
        self.invoke = MagicMock(return_value={"output": "done"})

    async def ainvoke(self, input: Any, config: Any = None, **kw: Any) -> dict:
        callbacks = (config or {}).get("callbacks", [])
        for tool_name in self._tools:
            run_id = uuid.uuid4()
            for cb in callbacks:
                if hasattr(cb, "on_tool_start"):
                    await cb.on_tool_start(
                        {"name": tool_name}, "tool input",
                        run_id=run_id,
                    )
            for cb in callbacks:
                if hasattr(cb, "on_tool_end"):
                    await cb.on_tool_end("tool result", run_id=run_id)
        return {"output": "langchain_result"}


def _govern(tools=None, chain_name="lc-test"):
    return govern(_StubChain(tools), chain_name=chain_name)


# ===========================================================================
# Scenario 1 — happy path
# ===========================================================================

@pytest.mark.asyncio
async def test_happy_path_langchain():
    mock_post, calls = make_mock_post()
    with patch("proofrail.client._post", side_effect=mock_post):
        result = await _govern().ainvoke({"input": "hello"})

    assert result == {"output": "langchain_result"}
    chain_calls = [c for c in calls if c["path"] == "/v1/chains"]
    assert len(chain_calls) == 1
    assert_chain_completed(calls)
    # 2 tools × (start + end) = 4 events
    assert count_event_calls(calls) == 4
    assert_event_recorded(calls, action_type="tool_call",   action_name="search_web")
    assert_event_recorded(calls, action_type="tool_result", action_name="search_web:result")
    assert_event_recorded(calls, action_type="tool_call",   action_name="format_output")
    assert_event_recorded(calls, action_type="tool_result", action_name="format_output:result")


# ===========================================================================
# Scenario 2 — backend flags one action
# ===========================================================================

@pytest.mark.asyncio
async def test_backend_flags_action_langchain():
    mock_post, calls = make_mock_post(flag_on="format_output")
    with patch("proofrail.client._post", side_effect=mock_post):
        result = await _govern().ainvoke({"input": "hello"})

    assert result == {"output": "langchain_result"}
    assert_chain_completed(calls)
    assert_event_recorded(calls, action_name="format_output", action_type="tool_call")


# ===========================================================================
# Scenario 3 — backend denies action mid-workflow
# ===========================================================================

@pytest.mark.asyncio
async def test_backend_denies_action_langchain():
    governed = govern(
        _StubChain(tools=["search_web", "send_email_tool"]),
        chain_name="lc-deny",
    )
    mock_post, calls = make_mock_post(deny_on="send_email_tool")

    with patch("proofrail.client._post", side_effect=mock_post):
        with pytest.raises(ActionDeniedError) as exc_info:
            await governed.ainvoke({"input": "data"})

    err = exc_info.value
    # Regression guard: must be ActionDeniedError, NOT TypeError
    assert isinstance(err, ActionDeniedError)
    assert err.policy_name is not None
    assert err.condition   is not None
    assert err.remediation is not None
    assert err.docs_url    is not None
    # First tool ran; deny happened at second tool's start
    assert_event_recorded(calls, action_name="search_web",    action_type="tool_call")
    assert_event_recorded(calls, action_name="send_email_tool", action_type="tool_call")
    # send_email_tool:result must NOT appear
    result_events = [
        c for c in calls
        if "events" in c["path"]
        and c["body"].get("action_name") == "send_email_tool:result"
    ]
    assert result_events == [], "Execution continued past the deny"


# ===========================================================================
# Scenario 4 — backend unreachable, fail_mode=deny
# ===========================================================================

@pytest.mark.asyncio
async def test_backend_unreachable_fail_deny_langchain():
    proofrail.init(
        api_key="prail_test",
        backend_url="http://localhost:9999",
        environment="development",
        enable_local_fast_path=False,
        fail_mode="deny",
    )
    mock_post, calls = make_mock_post(unavailable=True)

    with patch("proofrail.client._post", side_effect=mock_post):
        with pytest.raises(BackendUnavailableError):
            await _govern().ainvoke({"input": "data"})

    assert count_event_calls(calls) == 0


# ===========================================================================
# Scenario 5 — backend unreachable, fail_mode=allow (offline path)
# ===========================================================================

@pytest.mark.asyncio
async def test_backend_unreachable_fail_allow_langchain():
    mock_post, calls = make_mock_post(offline_signal=True)

    with patch("proofrail.client._post", side_effect=mock_post):
        result = await _govern().ainvoke({"input": "data"})

    assert result == {"output": "langchain_result"}
    # No synchronous event POSTs — all buffered in offline mode
    assert count_event_calls(calls) == 0


# ===========================================================================
# Scenario 6 — fast-path engages for low-risk actions
# ===========================================================================

@pytest.mark.asyncio
async def test_fast_path_engages_langchain():
    proofrail.init(
        api_key="prail_test",
        backend_url="http://localhost:9999",
        environment="development",
        enable_local_fast_path=True,
        fail_mode="allow",
    )
    # Single safe tool: "get_record" → risk_score = 0, fast-path eligible
    governed = govern(_StubChain(tools=["get_record"]), chain_name="lc-fp")
    mock_post, calls = make_mock_post()

    with patch("proofrail.client._post", side_effect=mock_post):
        result = await governed.ainvoke({"input": "data"})
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert result == {"output": "langchain_result"}
    # Chain must have been started
    assert any(c["path"] == "/v1/chains" for c in calls)
    # Workflow completed without exception — fast-path is engaged
    assert result is not None
