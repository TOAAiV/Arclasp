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
from proofrail.exceptions import ActionDeniedError, BackendUnavailableError, ChainTimeoutError

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


# ===========================================================================
# BUG-LC-02 — LangChain adapter must propagate policy exceptions via
#             BaseException pivot (same mechanism as BUG-LG-02 / Strategy B)
# ===========================================================================

class _StubChainWithSwallow:
    """
    Forces the callback-swallow scenario.  Wraps each callback invocation in
    ``try/except Exception: pass`` to directly simulate LangChain's callback
    manager swallowing behaviour.

    Pre-fix: ChainTimeoutError (inherits Exception) is caught and discarded;
    ainvoke() returns normally — the bug.
    Post-fix: _StrategyBPolicyBreak (inherits BaseException) escapes the
    except-Exception guard and propagates to the adapter for unwrapping.
    """

    def __init__(self, tool_name: str = "deny_tool") -> None:
        self._tool_name = tool_name
        self.invoke = MagicMock(return_value={"output": "done"})

    async def ainvoke(self, input: Any, config: Any = None, **kw: Any) -> dict:
        callbacks = (config or {}).get("callbacks", [])
        run_id = uuid.uuid4()
        for cb in callbacks:
            if hasattr(cb, "on_tool_start"):
                try:
                    await cb.on_tool_start(
                        {"name": self._tool_name}, "tool input",
                        run_id=run_id,
                    )
                except Exception:
                    pass  # simulates LangChain callback manager swallowing
                # _StrategyBPolicyBreak(BaseException) propagates here if fix applied
        return {"output": "should_not_reach_caller"}


@pytest.mark.asyncio
async def test_bug_lc_02_strategy_b_propagates_policy_exception():
    """
    BUG-LC-02: LangChain's callback manager catches Exception from
    BaseCallbackHandler methods, silently swallowing policy decisions.
    The fix raises _StrategyBPolicyBreak(BaseException) inside the callback,
    which escapes the except-Exception guard.  The adapter catches it and
    re-raises the original policy exception so the caller receives the correct
    signal.

    _StubChainWithSwallow encodes the exact bug: callbacks are wrapped in
    try/except Exception: pass.  Without the fix the caller sees no error.
    With the fix ChainTimeoutError propagates intact.
    """

    proofrail.init(
        api_key="prail_test",
        backend_url="http://localhost:9999",
        environment="development",
        enable_local_fast_path=False,
        fail_mode="allow",
        default_approval_timeout_hours=0,   # instant ChainTimeoutError on require_approval
    )

    require_approval_resp = {
        "policy_decision": "require_approval",
        "decision_reason": "High-risk tool blocked for approval",
        "decision_source": "backend_evaluation",
    }

    async def mock_post(path: str, body: dict, action_type: str | None = None) -> dict:
        if path == "/v1/chains":
            return {"id": "chain-lc02-swallow"}
        if path.endswith("/complete"):
            return {}
        action_name = (body or {}).get("action_name", "")
        if action_name == "deny_tool":
            return require_approval_resp
        return {"policy_decision": "allow", "decision_source": "backend_evaluation"}

    governed = govern(_StubChainWithSwallow(), chain_name="bug-lc-02")

    with patch("proofrail.client._post", side_effect=mock_post):
        with pytest.raises(ChainTimeoutError):
            await governed.ainvoke({"input": "test"})


# ===========================================================================
# BUG-LC-02 (high-fidelity) — BaseException escapes real langchain-core
# ===========================================================================

@pytest.mark.asyncio
async def test_bug_lc_02_base_exception_escapes_real_langchain_core():
    """
    Higher-fidelity regression for BUG-LC-02.

    test_bug_lc_02_strategy_b_propagates_policy_exception uses a hand-written
    try/except Exception: pass wrapper to simulate LangChain's callback manager.
    This test invokes langchain-core's REAL _ahandle_event_for_handler with our
    actual ProofRailLangChainCallback (which inherits real BaseCallbackHandler).

    Verifies that _StrategyBPolicyBreak(BaseException) escapes langchain-core's
    ``except Exception`` clause intact — if langchain-core ever adds
    ``except BaseException``, this test regresses before customers are affected.
    """
    import sys
    import uuid as _uuid
    import importlib


    # Remove conftest stubs so real langchain-core is importable.
    saved_lc = {k: sys.modules.pop(k)
                for k in list(sys.modules.keys())
                if k.startswith("langchain_core")}

    try:
        from langchain_core.callbacks.manager import _ahandle_event_for_handler

        # Reload our callbacks module so _BaseCallbackHandler becomes the real
        # langchain_core.callbacks.base.BaseCallbackHandler (not the stub).
        saved_cb_mod = sys.modules.pop("proofrail.langchain.callbacks", None)
        import proofrail.langchain.callbacks as _fresh_cb_mod
        importlib.reload(_fresh_cb_mod)

        FreshCallback = _fresh_cb_mod.ProofRailLangChainCallback
        FreshStrategyBPolicyBreak = _fresh_cb_mod._StrategyBPolicyBreak

        proofrail.init(
            api_key="prail_test",
            backend_url="http://localhost:9999",
            environment="development",
            enable_local_fast_path=False,
            fail_mode="allow",
            default_approval_timeout_hours=0,
        )

        require_approval_resp = {
            "policy_decision": "require_approval",
            "decision_reason": "Real langchain-core LangChain test",
            "decision_source": "backend_evaluation",
        }

        async def mock_post(path: str, body: dict, action_type: str | None = None) -> dict:
            if path == "/v1/chains":
                return {"id": "chain-lc02-real"}
            if path.endswith("/complete"):
                return {}
            action_name = (body or {}).get("action_name", "")
            if action_name == "real_lc_tool":
                return require_approval_resp
            return {"policy_decision": "allow", "decision_source": "backend_evaluation"}

        with patch("proofrail.client._post", side_effect=mock_post):
            async with proofrail.Chain(name="real-lc-test") as chain:
                callback = FreshCallback(chain, agent_name="test_agent")
                run_id = _uuid.uuid4()

                # Pass the callback directly — _ahandle_event_for_handler uses
                # duck-typing (getattr) so real AsyncCallbackHandler inheritance
                # is not required; the method just needs to be async and present.
                with pytest.raises(FreshStrategyBPolicyBreak) as exc_info:
                    await _ahandle_event_for_handler(
                        callback,
                        "on_tool_start",
                        None,                        # ignore_condition_name
                        {"name": "real_lc_tool"},    # serialized
                        "tool input",                # input_str
                        run_id=run_id,
                    )

                assert isinstance(exc_info.value.original, ChainTimeoutError)

    finally:
        # Restore stubs and original module state
        for k in list(sys.modules.keys()):
            if k.startswith("langchain_core"):
                del sys.modules[k]
        sys.modules.update(saved_lc)
        # Restore original callbacks module so other tests see stub base class
        if saved_cb_mod is not None:
            sys.modules["proofrail.langchain.callbacks"] = saved_cb_mod
        elif "proofrail.langchain.callbacks" in sys.modules:
            del sys.modules["proofrail.langchain.callbacks"]


# ===========================================================================
# EXPECTED-UNDOCUMENTED-LC-01 — LangChain adapter populates parent_agent_name
#   via Option B+: executor is the implicit parent of all tool/LLM events.
# ===========================================================================

class _StubChainWithParentIds:
    """
    Stub chain that fires two tool sequences to exercise all parent_run_id paths:
      - tool_no_parent    fired with parent_run_id=None     (path 1: implicit executor parent)
      - tool_unknown_parent fired with parent_run_id=<UUID not in _all_runs>  (path 2: Option B fallback)
    """

    def __init__(self) -> None:
        self.invoke = MagicMock(return_value={"output": "done"})

    async def ainvoke(self, input: Any, config: Any = None, **kw: Any) -> dict:
        callbacks = (config or {}).get("callbacks", [])

        # path 1: no parent_run_id → executor is implicit parent
        run_a = uuid.uuid4()
        for cb in callbacks:
            if hasattr(cb, "on_tool_start"):
                await cb.on_tool_start({"name": "tool_no_parent"}, "input a", run_id=run_a)
        for cb in callbacks:
            if hasattr(cb, "on_tool_end"):
                await cb.on_tool_end("result a", run_id=run_a)

        # path 2: explicit parent_run_id that is NOT in _all_runs (unknown executor chain run)
        unknown_parent = uuid.uuid4()
        run_b = uuid.uuid4()
        for cb in callbacks:
            if hasattr(cb, "on_tool_start"):
                await cb.on_tool_start(
                    {"name": "tool_unknown_parent"}, "input b",
                    run_id=run_b, parent_run_id=unknown_parent,
                )
        for cb in callbacks:
            if hasattr(cb, "on_tool_end"):
                await cb.on_tool_end("result b", run_id=run_b)

        return {"output": "lc_parent_sim"}


@pytest.mark.asyncio
async def test_strategy_b_populates_parent_agent_name_simulated():
    """
    EXPECTED-UNDOCUMENTED-LC-01 (simulated paths 1 and 2).

    Option B+ semantics: every tool event gets parent_agent_name = executor name,
    regardless of whether parent_run_id is None (path 1) or an unknown UUID (path 2).
    The _all_runs resolution path (path 3) is covered in the real langchain-core test.
    """
    mock_post, calls = make_mock_post()
    governed = govern(_StubChainWithParentIds(), chain_name="lc-parent-sim")

    with patch("proofrail.client._post", side_effect=mock_post):
        result = await governed.ainvoke({"input": "test"})

    assert result == {"output": "lc_parent_sim"}

    event_calls = [c for c in calls if "events" in c["path"]]
    assert len(event_calls) == 4  # 2 tools × (start + end)

    # govern() uses type(chain).__name__ as agent_name
    executor_name = "_StubChainWithParentIds"

    for ec in event_calls:
        body = ec["body"]
        assert body.get("parent_agent_name") == executor_name, (
            f"Expected parent_agent_name={executor_name!r} but got "
            f"{body.get('parent_agent_name')!r} in event {body.get('action_name')!r}"
        )


@pytest.mark.asyncio
async def test_strategy_b_populates_parent_agent_name_real():
    """
    EXPECTED-UNDOCUMENTED-LC-01 (real langchain-core paths 2 and 3).

    Uses real langchain-core's _ahandle_event_for_handler (same pattern as the
    BUG-LC-02 high-fidelity test) to verify that parent_agent_name resolution
    works through the actual callback dispatcher:

      path 2: parent_run_id=<UUID not in _all_runs> → falls back to agent_name
      path 3: parent_run_id=<UUID pre-registered in _all_runs> → resolves to that name
    """
    import sys
    import uuid as _uuid
    import importlib

    saved_lc = {k: sys.modules.pop(k)
                for k in list(sys.modules.keys())
                if k.startswith("langchain_core")}

    try:
        from langchain_core.callbacks.manager import _ahandle_event_for_handler

        saved_cb_mod = sys.modules.pop("proofrail.langchain.callbacks", None)
        import proofrail.langchain.callbacks as _fresh_cb_mod
        importlib.reload(_fresh_cb_mod)

        FreshCallback = _fresh_cb_mod.ProofRailLangChainCallback

        proofrail.init(
            api_key="prail_test",
            backend_url="http://localhost:9999",
            environment="development",
            enable_local_fast_path=False,
            fail_mode="allow",
        )

        async def mock_post(path: str, body: dict, action_type: str | None = None) -> dict:
            if path == "/v1/chains":
                return {"id": "chain-parent-real"}
            if path.endswith("/complete"):
                return {}
            return {"policy_decision": "allow", "decision_source": "backend_evaluation"}

        with patch("proofrail.client._post", side_effect=mock_post) as mock_p:
            async with proofrail.Chain(name="real-parent-test") as chain:
                callback = FreshCallback(chain, agent_name="test_executor")

                # path 2: unknown parent_run_id → fallback to "test_executor"
                unknown_parent = _uuid.uuid4()
                run_a = _uuid.uuid4()
                await _ahandle_event_for_handler(
                    callback,
                    "on_tool_start",
                    None,
                    {"name": "tool_unknown_parent"},
                    "input a",
                    run_id=run_a,
                    parent_run_id=unknown_parent,
                )

                # path 3: known parent pre-registered in _all_runs → resolves to its name
                parent_run = _uuid.uuid4()
                callback._all_runs[parent_run] = "parent_tool_name"
                run_b = _uuid.uuid4()
                await _ahandle_event_for_handler(
                    callback,
                    "on_tool_start",
                    None,
                    {"name": "child_tool"},
                    "input b",
                    run_id=run_b,
                    parent_run_id=parent_run,
                )

        # Collect event bodies from mock_post call args
        event_bodies: dict[str, dict] = {}
        for call_args in mock_p.call_args_list:
            path = call_args[0][0]
            body = call_args[0][1]
            if "events" in path:
                action_name = (body or {}).get("action_name", "")
                event_bodies[action_name] = body

        # path 2: unknown parent → fallback to agent_name
        assert "tool_unknown_parent" in event_bodies, "tool_unknown_parent event not recorded"
        assert event_bodies["tool_unknown_parent"]["parent_agent_name"] == "test_executor", (
            f"path 2 failed: expected 'test_executor', "
            f"got {event_bodies['tool_unknown_parent'].get('parent_agent_name')!r}"
        )

        # path 3: known parent in _all_runs → resolved to registered name
        assert "child_tool" in event_bodies, "child_tool event not recorded"
        assert event_bodies["child_tool"]["parent_agent_name"] == "parent_tool_name", (
            f"path 3 failed: expected 'parent_tool_name', "
            f"got {event_bodies['child_tool'].get('parent_agent_name')!r}"
        )

    finally:
        for k in list(sys.modules.keys()):
            if k.startswith("langchain_core"):
                del sys.modules[k]
        sys.modules.update(saved_lc)
        if saved_cb_mod is not None:
            sys.modules["proofrail.langchain.callbacks"] = saved_cb_mod
        elif "proofrail.langchain.callbacks" in sys.modules:
            del sys.modules["proofrail.langchain.callbacks"]
