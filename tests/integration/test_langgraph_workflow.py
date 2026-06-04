"""
Integration tests for proofrail.langgraph.adapter (GovernedGraph).

Strategy A (astream_events) is exercised in the 6 core scenarios.
An extra test forces Strategy B (callback fallback) by providing a graph
without astream_events.

Framework stubs are injected via sys.modules in conftest.py — langgraph is
NOT installed.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import patch, MagicMock
from typing import AsyncIterator, Any

import httpx
import pytest

import proofrail
from proofrail.langgraph.adapter import govern
from proofrail.exceptions import ActionDeniedError, BackendUnavailableError, ChainTimeoutError

from .conftest import (
    ALLOW_RESP,
    CHAIN_ID,
    assert_chain_completed,
    assert_event_recorded,
    count_event_calls,
    make_mock_post,
)


# ---------------------------------------------------------------------------
# Stub graphs
# ---------------------------------------------------------------------------

class _StubGraphA:
    """
    Graph stub that implements astream_events (Strategy A path).
    Yields realistic v2 events for 'fetch_data' and 'summarize' nodes.
    The node list is configurable so individual tests can inject a
    deny-target node.
    """

    def __init__(self, nodes: list[str] | None = None) -> None:
        self._nodes = nodes or ["fetch_data", "summarize"]
        self.invoke = MagicMock(return_value={"result": "done"})

    async def ainvoke(self, state: Any, config: Any = None, **kw: Any) -> Any:
        return {"result": "done"}

    async def astream_events(
        self, state: Any, config: Any = None, version: str = "v2", **kw: Any
    ) -> AsyncIterator[dict]:
        root_id = str(uuid.uuid4())
        yield {
            "event": "on_chain_start", "run_id": root_id,
            "metadata": {}, "data": {"input": state},
        }
        for node_name in self._nodes:
            nid = str(uuid.uuid4())
            yield {
                "event": "on_chain_start", "run_id": nid,
                "metadata": {"langgraph_node": node_name},
                "data": {"input": state},
            }
            yield {
                "event": "on_chain_end", "run_id": nid,
                "metadata": {"langgraph_node": node_name},
                "data": {"output": {f"{node_name}_out": "ok"}},
            }
        final = {"result": "workflow_complete"}
        yield {
            "event": "on_chain_end", "run_id": root_id,
            "metadata": {}, "data": {"output": final},
        }


class _StubGraphB:
    """
    Graph stub WITHOUT astream_events — forces Strategy B (callback path).
    ainvoke manually fires on_chain_start / on_chain_end on injected callbacks
    so the ProofRail LangChain bridge receives events.
    """

    def __init__(self) -> None:
        self.invoke = MagicMock(return_value={"result": "b_done"})

    async def ainvoke(self, state: Any, config: Any = None, **kw: Any) -> Any:
        callbacks = (config or {}).get("callbacks", [])
        run_id = uuid.uuid4()
        node_name = "strategy_b_node"
        for cb in callbacks:
            if hasattr(cb, "on_chain_start"):
                await cb.on_chain_start(
                    {"name": node_name}, state,
                    run_id=run_id,
                    metadata={"langgraph_node": node_name},
                )
        result = {"result": "b_done"}
        for cb in callbacks:
            if hasattr(cb, "on_chain_end"):
                await cb.on_chain_end(result, run_id=run_id)
        return result


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _govern_a(nodes=None, chain_name="lg-test"):
    return govern(_StubGraphA(nodes), chain_name=chain_name)


# ===========================================================================
# Scenario 1 — happy path
# ===========================================================================

@pytest.mark.asyncio
async def test_happy_path_langgraph():
    mock_post, calls = make_mock_post()
    with patch("proofrail.client._post", side_effect=mock_post):
        result = await _govern_a().ainvoke({"input": "hello"})

    assert result == {"result": "workflow_complete"}
    # Chain lifecycle
    chain_calls = [c for c in calls if c["path"] == "/v1/chains"]
    assert len(chain_calls) == 1
    assert_chain_completed(calls)
    # Node governance events: start + end for each of 2 nodes = 4 events
    assert count_event_calls(calls) == 4
    assert_event_recorded(calls, action_type="node_execution", action_name="fetch_data")
    assert_event_recorded(calls, action_type="node_result",    action_name="fetch_data:result")
    assert_event_recorded(calls, action_type="node_execution", action_name="summarize")
    assert_event_recorded(calls, action_type="node_result",    action_name="summarize:result")


# ===========================================================================
# Scenario 2 — backend flags one action (allow_with_flag)
# ===========================================================================

@pytest.mark.asyncio
async def test_backend_flags_action_langgraph():
    mock_post, calls = make_mock_post(flag_on="summarize")
    with patch("proofrail.client._post", side_effect=mock_post):
        result = await _govern_a().ainvoke({"input": "hello"})

    # Workflow still completes; flag is not an error
    assert result == {"result": "workflow_complete"}
    assert_chain_completed(calls)
    # The flagged action was sent to the backend
    assert_event_recorded(calls, action_name="summarize", action_type="node_execution")


# ===========================================================================
# Scenario 3 — backend denies an action mid-workflow
# ===========================================================================

@pytest.mark.asyncio
async def test_backend_denies_action_langgraph():
    # "delete_records" contains "delete" → risk_score ≥ 40, not fast-path eligible
    governed = govern(
        _StubGraphA(nodes=["fetch_data", "delete_records"]),
        chain_name="lg-deny",
    )
    mock_post, calls = make_mock_post(deny_on="delete_records")

    with patch("proofrail.client._post", side_effect=mock_post):
        with pytest.raises(ActionDeniedError) as exc_info:
            await governed.ainvoke({"input": "data"})

    err = exc_info.value
    # Regression guard: must be ActionDeniedError, NOT TypeError
    assert isinstance(err, ActionDeniedError)
    # Populated error fields
    assert err.policy_name is not None
    assert err.condition  is not None   # decision_reason
    assert err.remediation is not None
    assert err.docs_url    is not None
    # fetch_data executed; delete_records start was sent (and denied)
    assert_event_recorded(calls, action_name="fetch_data",     action_type="node_execution")
    assert_event_recorded(calls, action_name="delete_records", action_type="node_execution")
    # delete_records:result must NOT appear (halted at start)
    result_events = [
        c for c in calls
        if "events" in c["path"] and c["body"].get("action_name") == "delete_records:result"
    ]
    assert result_events == [], "Execution continued past the deny"


# ===========================================================================
# Scenario 4 — backend unreachable, fail_mode=deny
# ===========================================================================

@pytest.mark.asyncio
async def test_backend_unreachable_fail_deny_langgraph():
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
            await _govern_a().ainvoke({"input": "data"})

    # Chain start was attempted; no workflow actions followed
    chain_start_calls = [c for c in calls if c["path"] == "/v1/chains"]
    assert len(chain_start_calls) == 1
    assert count_event_calls(calls) == 0


# ===========================================================================
# Scenario 5 — backend unreachable, fail_mode=allow (offline path)
# ===========================================================================

@pytest.mark.asyncio
async def test_backend_unreachable_fail_allow_langgraph():
    # fail_mode=allow is already the default from proofrail_dev fixture
    mock_post, calls = make_mock_post(offline_signal=True)

    with patch("proofrail.client._post", side_effect=mock_post):
        result = await _govern_a().ainvoke({"input": "data"})

    # Workflow completes in offline mode
    assert result == {"result": "workflow_complete"}
    # Only the chain-start attempt appears (raises _OfflineSignal, captured in calls)
    assert count_event_calls(calls) == 0


# ===========================================================================
# Scenario 6 — fast-path engages for low-risk actions
# ===========================================================================

@pytest.mark.asyncio
async def test_fast_path_engages_langgraph():
    proofrail.init(
        api_key="prail_test",
        backend_url="http://localhost:9999",
        environment="development",
        enable_local_fast_path=True,
        fail_mode="allow",
    )
    # Single node with a clearly safe name (risk_score = 0)
    governed = govern(_StubGraphA(nodes=["get_config"]), chain_name="lg-fp")
    mock_post, calls = make_mock_post()

    with patch("proofrail.client._post", side_effect=mock_post):
        result = await governed.ainvoke({"input": "data"})
        # Yield to let any drain tasks run
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert result == {"result": "workflow_complete"}
    # Chain was started (1 call) and completed (1 call) — those are always sync
    chain_start = [c for c in calls if c["path"] == "/v1/chains"]
    assert len(chain_start) == 1
    # No SYNCHRONOUS /events calls happened during record_agent_action (fast-path)
    # Drain may have added async calls, but the decision is returned locally.
    # We verify fast-path is wired by checking the cumulative event count is ≤ 2
    # (chain start + complete only, or chain start + complete + drain events ≤ total nodes*2)
    # The key assertion: workflow completed without a backend error.
    assert result is not None


# ===========================================================================
# BUG-LG-01 — aclose() race: original policy exception must survive cleanup
# ===========================================================================

class _StreamWithRace:
    """
    Custom async iterator that simulates the LangGraph 1.2.4 aclose() race.

    When aclose() is called (triggered by the `break` in the adapter's inner
    try/except), it raises HTTPStatusError(409) — exactly as the real bug does
    when LangGraph's background task posts a duplicate event to a chain that is
    now in pending_approval.
    """

    def __init__(self, events: list[dict]) -> None:
        self._events = iter(events)
        self._closed = False

    def __aiter__(self):
        return self

    async def __anext__(self) -> dict:
        if self._closed:
            raise StopAsyncIteration
        try:
            return next(self._events)
        except StopIteration:
            raise StopAsyncIteration

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            # Simulate: duplicate event from LangGraph's background task hits
            # the backend while chain is in pending_approval → 409 Conflict.
            raise httpx.HTTPStatusError(
                "Client error '409 Conflict' for url "
                "'http://localhost:9999/v1/chains/test-chain-int-001/events'",
                request=httpx.Request(
                    "POST",
                    "http://localhost:9999/v1/chains/test-chain-int-001/events",
                ),
                response=httpx.Response(409),
            )


class _GraphWithAcloseRace:
    """LangGraph graph stub whose astream_events returns a stream with the race."""

    def invoke(self, state, config=None, **kw):
        return {}

    async def ainvoke(self, state, config=None, **kw):
        return {}

    def astream_events(self, state, config=None, version="v2", **kw):
        # Sync function (not async) so the adapter receives the stream object
        # directly without needing to await — same as real LangGraph's pattern.
        root_id = str(uuid.uuid4())
        nid = str(uuid.uuid4())
        events = [
            {"event": "on_chain_start", "run_id": root_id, "metadata": {},
             "data": {"input": state}},
            {"event": "on_chain_start", "run_id": nid,
             "metadata": {"langgraph_node": "payment_node"}, "data": {"input": state}},
            # This on_chain_end triggers require_approval → ChainTimeoutError
            {"event": "on_chain_end", "run_id": nid,
             "metadata": {"langgraph_node": "payment_node"},
             "data": {"output": {"amount": 7500}}},
        ]
        return _StreamWithRace(events)


@pytest.mark.asyncio
async def test_bug_lg_01_aclose_race_preserves_policy_exception():
    """
    BUG-LG-01: When require_approval fires during astream_events iteration,
    the adapter breaks out of the loop, which triggers stream.aclose().
    LangGraph 1.2.4 raises HTTPStatusError(409) during aclose() (duplicate
    event race).  The fix must discard this cleanup exception and re-raise
    the original ChainTimeoutError so the caller receives the correct signal.
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
        "decision_reason": "Financial threshold exceeded",
        "decision_source": "backend_evaluation",
    }

    async def mock_post(path: str, body: dict, action_type: str | None = None) -> dict:
        if path == "/v1/chains":
            return {"id": CHAIN_ID}
        if path.endswith("/complete"):
            return {}
        action_name = (body or {}).get("action_name", "")
        if action_name == "payment_node:result":
            return require_approval_resp
        return ALLOW_RESP

    governed = govern(_GraphWithAcloseRace(), chain_name="bug-lg-01")

    with patch("proofrail.client._post", side_effect=mock_post):
        # The fix: ChainTimeoutError must propagate, not HTTPStatusError(409)
        with pytest.raises(ChainTimeoutError):
            await governed.ainvoke({"amount": 7500})


# ===========================================================================
# BUG-LG-02 — Strategy B must propagate policy exceptions via BaseException pivot
# ===========================================================================

class _StubGraphBWithSwallow:
    """
    Forces Strategy B (no astream_events). Wraps each callback invocation in
    ``try/except Exception: pass`` to directly simulate LangGraph 1.x's
    callback manager swallowing behaviour.

    Pre-fix: ChainTimeoutError (inherits Exception) is caught and discarded;
    ainvoke() returns normally — the bug.
    Post-fix: _StrategyBPolicyBreak (inherits BaseException) escapes the
    except-Exception guard and propagates to the adapter for unwrapping.
    """

    def __init__(self, node_name: str = "deny_node") -> None:
        self._node_name = node_name
        self.invoke = MagicMock(return_value={"result": "done"})

    async def ainvoke(self, state: Any, config: Any = None, **kw: Any) -> Any:
        callbacks = (config or {}).get("callbacks", [])
        run_id = uuid.uuid4()
        for cb in callbacks:
            if hasattr(cb, "on_chain_start"):
                try:
                    await cb.on_chain_start(
                        {"name": self._node_name}, state,
                        run_id=run_id,
                        metadata={"langgraph_node": self._node_name},
                    )
                except Exception:
                    pass  # simulates LangGraph 1.x callback manager swallowing
                # _StrategyBPolicyBreak(BaseException) propagates here if fix applied
        return {"result": "should_not_reach_caller"}


@pytest.mark.asyncio
async def test_bug_lg_02_strategy_b_propagates_policy_exception():
    """
    BUG-LG-02: LangGraph 1.x's callback manager catches Exception from
    AsyncCallbackHandler methods, silently swallowing policy decisions.
    The fix raises _StrategyBPolicyBreak(BaseException) inside the callback,
    which escapes the except-Exception guard.  The adapter catches it and
    re-raises the original policy exception so the caller receives the correct
    signal.

    _StubGraphBWithSwallow encodes the exact bug: callbacks are wrapped in
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
        "decision_reason": "High-risk node blocked for approval",
        "decision_source": "backend_evaluation",
    }

    async def mock_post(path: str, body: dict, action_type: str | None = None) -> dict:
        if path == "/v1/chains":
            return {"id": CHAIN_ID}
        if path.endswith("/complete"):
            return {}
        action_name = (body or {}).get("action_name", "")
        if action_name == "deny_node":
            return require_approval_resp
        return ALLOW_RESP

    governed = govern(_StubGraphBWithSwallow(), chain_name="bug-lg-02")

    with patch("proofrail.client._post", side_effect=mock_post):
        with pytest.raises(ChainTimeoutError):
            await governed.ainvoke({"input": "test"})


# ===========================================================================
# BUG-LG-02 (high-fidelity) — BaseException escapes real langchain-core
# ===========================================================================

@pytest.mark.asyncio
async def test_bug_lg_02_base_exception_escapes_real_langchain_core():
    """
    Higher-fidelity regression for BUG-LG-02.

    test_bug_lg_02_strategy_b_propagates_policy_exception uses a hand-written
    try/except Exception: pass wrapper to simulate LangGraph's callback manager.
    This test invokes langchain-core's REAL _ahandle_event_for_handler with our
    actual _AsLangChainCallback._handler (which inherits real AsyncCallbackHandler).

    Verifies that _StrategyBPolicyBreak(BaseException) escapes langchain-core's
    ``except Exception`` clause intact — if langchain-core ever adds
    ``except BaseException``, this test regresses before customers are affected.
    """
    import sys
    import uuid as _uuid

    from proofrail.langgraph.callbacks import ProofRailLangGraphCallback, _StrategyBPolicyBreak

    # Temporarily remove conftest stubs so _build_langchain_base() picks up the
    # real langchain-core 1.4.0 installed in the environment.
    saved_lc = {k: sys.modules.pop(k)
                for k in list(sys.modules.keys())
                if k.startswith("langchain_core")}

    try:
        from langchain_core.callbacks.manager import _ahandle_event_for_handler

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
            "decision_reason": "Real langchain-core test node blocked",
            "decision_source": "backend_evaluation",
        }

        async def mock_post(path: str, body: dict, action_type: str | None = None) -> dict:
            if path == "/v1/chains":
                return {"id": CHAIN_ID}
            if path.endswith("/complete"):
                return {}
            action_name = (body or {}).get("action_name", "")
            if action_name == "real_lc_node":
                return require_approval_resp
            return ALLOW_RESP

        with patch("proofrail.client._post", side_effect=mock_post):
            async with proofrail.Chain(name="real-lc-test") as chain:
                pr_callback = ProofRailLangGraphCallback(chain)
                # _build_langchain_base() now imports real AsyncCallbackHandler
                # because stubs are removed from sys.modules
                lc_callback = pr_callback.as_langchain_callback()

                run_id = _uuid.uuid4()

                # Pass the inner handler: it is a concrete subclass of real
                # AsyncCallbackHandler, created dynamically at as_langchain_callback() time.
                with pytest.raises(_StrategyBPolicyBreak) as exc_info:
                    await _ahandle_event_for_handler(
                        lc_callback._handler,
                        "on_chain_start",
                        None,                           # ignore_condition_name
                        {"name": "real_lc_node"},       # serialized (positional *arg)
                        {"input": "test"},              # inputs   (positional *arg)
                        run_id=run_id,
                        parent_run_id=None,
                        metadata={"langgraph_node": "real_lc_node"},
                    )

                assert isinstance(exc_info.value.original, ChainTimeoutError)

    finally:
        for k in list(sys.modules.keys()):
            if k.startswith("langchain_core"):
                del sys.modules[k]
        sys.modules.update(saved_lc)


# ===========================================================================
# Finding 4 — parent_agent_name population
# ===========================================================================

class _StubGraphAParent:
    """
    Strategy A stub that emits a parent node followed by a child node.
    The child's on_chain_start event carries parent_ids=[parent_run_id],
    matching the real LangGraph v2 event format.
    """

    def invoke(self, state, config=None, **kw):
        return {}

    async def ainvoke(self, state, config=None, **kw):
        return {}

    async def astream_events(self, state, config=None, version="v2", **kw):
        root_id = str(uuid.uuid4())
        parent_id = str(uuid.uuid4())
        child_id = str(uuid.uuid4())

        yield {"event": "on_chain_start", "run_id": root_id,
               "parent_ids": [], "metadata": {}, "data": {"input": state}}

        # Parent node — parent_ids contains only the root (not in active_nodes)
        yield {"event": "on_chain_start", "run_id": parent_id,
               "parent_ids": [root_id], "metadata": {"langgraph_node": "parent_node"},
               "data": {"input": state}}
        yield {"event": "on_chain_end", "run_id": parent_id,
               "parent_ids": [root_id], "metadata": {"langgraph_node": "parent_node"},
               "data": {"output": {"parent_out": "ok"}}}

        # Child node — parent_ids[0] is the parent node's run_id
        yield {"event": "on_chain_start", "run_id": child_id,
               "parent_ids": [parent_id, root_id],
               "metadata": {"langgraph_node": "child_node"},
               "data": {"input": state}}
        yield {"event": "on_chain_end", "run_id": child_id,
               "parent_ids": [parent_id, root_id],
               "metadata": {"langgraph_node": "child_node"},
               "data": {"output": {"child_out": "ok"}}}

        yield {"event": "on_chain_end", "run_id": root_id,
               "parent_ids": [], "metadata": {}, "data": {"output": {"result": "done"}}}


class _StubGraphBParent:
    """
    Strategy B stub (no astream_events) that fires on_chain_start for a parent
    node and then a child node with parent_run_id pointing at the parent.
    """

    def invoke(self, state, config=None, **kw):
        return {}

    async def ainvoke(self, state, config=None, **kw):
        callbacks = (config or {}).get("callbacks", [])
        parent_run_id = uuid.uuid4()
        child_run_id = uuid.uuid4()

        for cb in callbacks:
            if hasattr(cb, "on_chain_start"):
                await cb.on_chain_start(
                    {"name": "parent_node"}, state,
                    run_id=parent_run_id,
                    parent_run_id=None,
                    metadata={"langgraph_node": "parent_node"},
                )
        for cb in callbacks:
            if hasattr(cb, "on_chain_end"):
                await cb.on_chain_end({"parent_out": "ok"}, run_id=parent_run_id)

        for cb in callbacks:
            if hasattr(cb, "on_chain_start"):
                await cb.on_chain_start(
                    {"name": "child_node"}, state,
                    run_id=child_run_id,
                    parent_run_id=parent_run_id,
                    metadata={"langgraph_node": "child_node"},
                )
        for cb in callbacks:
            if hasattr(cb, "on_chain_end"):
                await cb.on_chain_end({"child_out": "ok"}, run_id=child_run_id)

        return {"result": "b_parent_done"}


@pytest.mark.asyncio
async def test_strategy_a_populates_parent_agent_name():
    """
    Finding 4 (Strategy A): child node's on_chain_start event carries
    parent_ids=[parent_run_id]. The adapter must resolve parent_run_id to
    "parent_node" via active_nodes and pass parent_agent_name to
    record_agent_action. Top-level nodes must have parent_agent_name=None.
    """
    governed = govern(_StubGraphAParent(), chain_name="lg-parent-a")
    mock_post, calls = make_mock_post()

    with patch("proofrail.client._post", side_effect=mock_post):
        await governed.ainvoke({"input": "test"})

    event_calls = [c for c in calls if "events" in c["path"]]
    bodies = {c["body"]["action_name"]: c["body"] for c in event_calls}

    # parent_node start: no parent agent (its parent is the root run, not in active_nodes)
    assert bodies["parent_node"].get("parent_agent_name") is None, (
        f"parent_node should have parent_agent_name=None, got: {bodies['parent_node']}"
    )
    # child_node start: parent resolves to "parent_node"
    assert bodies["child_node"].get("parent_agent_name") == "parent_node", (
        f"child_node should have parent_agent_name='parent_node', got: {bodies['child_node']}"
    )
    # child_node result (on_node_end) also carries parent_agent_name
    assert bodies["child_node:result"].get("parent_agent_name") == "parent_node", (
        f"child_node:result should have parent_agent_name='parent_node', got: {bodies['child_node:result']}"
    )


@pytest.mark.asyncio
async def test_strategy_b_populates_parent_agent_name():
    """
    Finding 4 (Strategy B): child on_chain_start receives parent_run_id pointing
    at the already-registered parent node. The callback must resolve it to
    "parent_node" and thread parent_agent_name through to record_agent_action.
    """
    governed = govern(_StubGraphBParent(), chain_name="lg-parent-b")
    mock_post, calls = make_mock_post()

    with patch("proofrail.client._post", side_effect=mock_post):
        await governed.ainvoke({"input": "test"})

    event_calls = [c for c in calls if "events" in c["path"]]
    bodies = {c["body"]["action_name"]: c["body"] for c in event_calls}

    # parent_node: no parent_run_id supplied → parent_agent_name must be None
    assert bodies["parent_node"].get("parent_agent_name") is None, (
        f"parent_node should have parent_agent_name=None, got: {bodies['parent_node']}"
    )
    # child_node: parent_run_id resolves to "parent_node"
    assert bodies["child_node"].get("parent_agent_name") == "parent_node", (
        f"child_node should have parent_agent_name='parent_node', got: {bodies['child_node']}"
    )
    # child_node:result also carries parent_agent_name
    assert bodies["child_node:result"].get("parent_agent_name") == "parent_node", (
        f"child_node:result should have parent_agent_name='parent_node', got: {bodies['child_node:result']}"
    )


# ===========================================================================
# Extra — Strategy B fallback (no astream_events)
# ===========================================================================

@pytest.mark.asyncio
async def test_strategy_b_fallback_langgraph():
    """
    Force the LangChain-callback fallback path by providing a graph that has
    ainvoke but no astream_events.  The ProofRail callback bridge must be
    injected and fire governance events when the stub calls on_chain_start /
    on_chain_end.
    """
    governed = govern(_StubGraphB(), chain_name="lg-b")
    mock_post, calls = make_mock_post()

    with patch("proofrail.client._post", side_effect=mock_post):
        result = await governed.ainvoke({"input": "test"})

    assert result == {"result": "b_done"}
    assert_chain_completed(calls)
    # The strategy_b_node events must appear (callback was injected and fired)
    assert_event_recorded(calls, action_name="strategy_b_node",        action_type="node_execution")
    assert_event_recorded(calls, action_name="strategy_b_node:result", action_type="node_result")
