"""
Integration tests for arclasp.crewai.adapter (GovernedCrew).

Strategy A (native before_task_callback / task_callback hooks) is exercised
in the 6 core scenarios.  An extra test forces Strategy B (monkey-patch
execute_task) and confirms that:
  - execute_task is intercepted and records governance events.
  - the original method is restored after the run (cleanup guarantee).

NOTE on CrewAI Scenario 3 (deny):
  _fire() now blocks synchronously via fut.result() from the worker thread,
  so a backend deny raises ActionDeniedError and halts the crew immediately.
  This matches LangGraph / LangChain behaviour.  Stubs dispatch through
  asyncio.to_thread so _fire() is always called from a worker thread.

Framework stubs are injected via sys.modules in conftest.py — crewai is
NOT installed.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch
from typing import Any

import pytest

import arclasp
from arclasp.crewai.adapter import govern
from arclasp.exceptions import ActionDeniedError, BackendUnavailableError

from .conftest import (
    make_mock_post,
    assert_event_recorded,
    assert_chain_completed,
    count_event_calls,
)

try:
    import crewai  # noqa: F401

    CREWAI_AVAILABLE = True
except ImportError:
    CREWAI_AVAILABLE = False

pytestmark = pytest.mark.skipif(not CREWAI_AVAILABLE, reason="crewai not installed")


# ---------------------------------------------------------------------------
# Stub helpers (< 30 lines combined)
# ---------------------------------------------------------------------------

class _MockTask:
    def __init__(self, description: str) -> None:
        self.description    = description
        self.expected_output = "Expected result"


class _MockAgent:
    def __init__(self, role: str) -> None:
        self.role = role

    def execute_task(self, task: Any, *args: Any, **kw: Any) -> str:
        return f"Result: {task.description}"


class _MockTaskOutput:
    def __init__(self, raw: str, agent: str, description: str = "") -> None:
        self.raw         = raw
        self.agent       = agent
        self.summary     = raw[:100]
        # description mirrors the required TaskOutput.description field in CrewAI 1.x;
        # without it, on_task_end_from_output falls back to the literal string "task".
        self.description = description


# ---------------------------------------------------------------------------
# Strategy A crew stub (has native CrewAI callbacks)
# ---------------------------------------------------------------------------

class _StubCrewA:
    """Crew with before_task_callback and task_callback (Strategy A path)."""

    def __init__(self, tasks: list[_MockTask] | None = None) -> None:
        self.agents = [_MockAgent("Researcher"), _MockAgent("Writer")]
        self.tasks  = tasks or [
            _MockTask("Research the market"),
            _MockTask("Write the report"),
        ]
        self.before_task_callback = None   # adapter wraps these
        self.task_callback        = None

    async def kickoff_async(self, inputs: Any = None, **kw: Any) -> list:
        # Dispatch each task into a worker thread, mirroring real CrewAI 1.x
        # (Crew.kickoff_async = asyncio.to_thread(kickoff)).  This ensures
        # _fire() is always called from a worker thread so fut.result() never
        # deadlocks the event loop.
        results = []
        for task, agent in zip(self.tasks, self.agents):
            before_cb = self.before_task_callback
            after_cb  = self.task_callback

            def _run(task=task, agent=agent, bcb=before_cb, acb=after_cb):
                if callable(bcb):
                    bcb(task, agent)
                out = _MockTaskOutput(
                    f"Result for {task.description}", agent.role,
                    description=task.description,
                )
                if callable(acb):
                    acb(out)
                return out

            out = await asyncio.to_thread(_run)
            results.append(out)
        return results

    def kickoff(self, inputs: Any = None, **kw: Any) -> Any:
        return asyncio.run(self.kickoff_async(inputs, **kw))


# ---------------------------------------------------------------------------
# Strategy B crew stub (no native callbacks → monkey-patch path)
# ---------------------------------------------------------------------------

class _StubCrewB:
    """Crew without native callbacks — forces Strategy B (execute_task patch)."""

    def __init__(self) -> None:
        self.agents = [_MockAgent("Analyst")]
        self.tasks  = [_MockTask("Analyse the data")]
        # NO before_task_callback / task_callback attributes

    async def kickoff_async(self, inputs: Any = None, **kw: Any) -> list:
        # Worker-thread dispatch mirrors real CrewAI 1.x so _fire() can block
        # safely without deadlocking the event loop.
        results = []
        for task, agent in zip(self.tasks, self.agents):
            def _run(task=task, agent=agent):
                # execute_task will be monkey-patched by the adapter
                return agent.execute_task(task)

            result = await asyncio.to_thread(_run)
            results.append(result)
        return results

    def kickoff(self, inputs: Any = None, **kw: Any) -> Any:
        return asyncio.run(self.kickoff_async(inputs, **kw))


# ---------------------------------------------------------------------------
# Mixed-callback crew stub (task_callback only — no before_task_callback)
# This is the exact shape of every CrewAI 1.x Crew instance.
# ---------------------------------------------------------------------------

class _StubCrewMixed:
    """
    Crew with task_callback but NO before_task_callback — the default shape
    of every CrewAI 1.x Crew instance (before_task_callback was removed in 1.x).

    Calls execute_task on each agent (so Strategy B's start-event patch fires),
    then calls self.task_callback with a TaskOutput-like object (so Strategy A's
    end-event wrapper fires).  Accurately simulates the CrewAI 1.x execution flow.
    """

    def __init__(self, tasks: list[_MockTask] | None = None) -> None:
        self.agents = [_MockAgent("Researcher"), _MockAgent("Writer")]
        self.tasks  = tasks or [
            _MockTask("Research the market"),
            _MockTask("Write the report"),
        ]
        self.task_callback = None   # has task_callback, but NO before_task_callback

    async def kickoff_async(self, inputs: Any = None, **kw: Any) -> list:
        # Worker-thread dispatch mirrors real CrewAI 1.x so _fire() can block
        # safely without deadlocking the event loop.
        results = []
        for task, agent in zip(self.tasks, self.agents):
            after_cb = self.task_callback

            def _run(task=task, agent=agent, acb=after_cb):
                # execute_task is patched by Strategy B → fires on_task_start only
                raw = agent.execute_task(task)
                out = _MockTaskOutput(
                    raw, agent.role,
                    description=task.description,
                )
                # task_callback is wrapped by Strategy A → fires on_task_end_from_output
                if callable(acb):
                    acb(out)
                return out

            out = await asyncio.to_thread(_run)
            results.append(out)
        return results

    def kickoff(self, inputs: Any = None, **kw: Any) -> Any:
        return asyncio.run(self.kickoff_async(inputs, **kw))


def _govern_a(tasks=None, chain_name="crew-test"):
    return govern(_StubCrewA(tasks), chain_name=chain_name)


# ===========================================================================
# Scenario 1 — happy path
# ===========================================================================

@pytest.mark.asyncio
async def test_happy_path_crewai():
    mock_post, calls = make_mock_post()
    with patch("arclasp.client._post", side_effect=mock_post):
        result = await _govern_a().kickoff_async(inputs={"topic": "AI"})
        # Give fire-and-forget coroutines time to run
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert len(result) == 2
    chain_calls = [c for c in calls if c["path"] == "/v1/chains"]
    assert len(chain_calls) == 1
    assert_chain_completed(calls)
    # 2 tasks × (start + end) = 4 governance events
    assert count_event_calls(calls) == 4
    assert_event_recorded(calls, action_type="task_execution")
    assert_event_recorded(calls, action_type="task_result")


# ===========================================================================
# Scenario 2 — backend flags one action
# ===========================================================================

@pytest.mark.asyncio
async def test_backend_flags_action_crewai():
    mock_post, calls = make_mock_post(flag_on="Research the market")
    with patch("arclasp.client._post", side_effect=mock_post):
        result = await _govern_a().kickoff_async(inputs={"topic": "AI"})
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert len(result) == 2
    assert_chain_completed(calls)
    assert_event_recorded(calls, action_name="Research the market", action_type="task_execution")


# ===========================================================================
# Scenario 3 — backend denies action
#
# CrewAI's fire-and-forget governance means denial does NOT halt execution.
# The deny event IS recorded; the crew completes.  See module docstring.
# ===========================================================================

@pytest.mark.asyncio
async def test_backend_denies_action_crewai():
    tasks = [
        _MockTask("Research the market"),
        _MockTask("Delete old records"),   # denied on the backend
    ]
    governed = govern(_StubCrewA(tasks=tasks), chain_name="crew-deny")
    # "Delete old records" → action_name[:100] = "Delete old records"
    mock_post, calls = make_mock_post(deny_on="Delete old records")

    with patch("arclasp.client._post", side_effect=mock_post):
        # ActionDeniedError now propagates — synchronous _fire() fix
        with pytest.raises(ActionDeniedError):
            await governed.kickoff_async(inputs={"topic": "test"})

    # Task 1 was allowed through — both start and end events were sent
    assert_event_recorded(calls, action_name="Research the market", action_type="task_execution")
    # Task 2's before-event was sent; the backend denied it, raising ActionDeniedError
    assert_event_recorded(calls, action_name="Delete old records", action_type="task_execution")
    # Task 2 was halted before completion — no task_result event for it
    task_2_results = [
        c for c in calls
        if "events" in c["path"]
        and c["body"].get("action_name", "").startswith("Delete old records")
        and c["body"].get("action_type") == "task_result"
    ]
    assert len(task_2_results) == 0, (
        f"Expected no task_result for denied task, got: {[c['body'] for c in task_2_results]}"
    )


# ===========================================================================
# Scenario 4 — backend unreachable, fail_mode=deny
# ===========================================================================

@pytest.mark.asyncio
async def test_backend_unreachable_fail_deny_crewai():
    arclasp.init(
        api_key="prail_test",
        backend_url="http://localhost:9999",
        environment="development",
        enable_local_fast_path=False,
        fail_mode="deny",
    )
    mock_post, calls = make_mock_post(unavailable=True)

    with patch("arclasp.client._post", side_effect=mock_post):
        with pytest.raises(BackendUnavailableError):
            await _govern_a().kickoff_async(inputs={"topic": "AI"})

    assert count_event_calls(calls) == 0


# ===========================================================================
# Scenario 5 - backend unreachable, deprecated fail_mode=allow fails closed
# ===========================================================================

@pytest.mark.asyncio
async def test_backend_unreachable_fail_allow_crewai():
    with pytest.warns(DeprecationWarning, match="fail_mode"):
        arclasp.init(
            api_key="prail_test",
            backend_url="http://localhost:9999",
            environment="development",
            enable_local_fast_path=False,
            fail_mode="allow",
        )
    mock_post, calls = make_mock_post(offline_signal=True)

    with patch("arclasp.client._post", side_effect=mock_post):
        with pytest.raises(BackendUnavailableError) as exc_info:
            await _govern_a().kickoff_async(inputs={"topic": "AI"})

    assert exc_info.value.fail_mode == "allow"
    assert count_event_calls(calls) == 0


# ===========================================================================
# Scenario 6 - deprecated deprecated fast-path flag still requires backend authority
# ===========================================================================

@pytest.mark.asyncio
async def test_fast_path_config_still_uses_backend_crewai():
    with pytest.warns(DeprecationWarning, match="enable_local_fast_path"):
        arclasp.init(
            api_key="prail_test",
            backend_url="http://localhost:9999",
            environment="development",
            enable_local_fast_path=True,
            fail_mode="deny",
        )
    tasks = [_MockTask("Get config data")]   # safe name → risk_score = 0
    governed = govern(_StubCrewA(tasks=tasks), chain_name="crew-fp")
    mock_post, calls = make_mock_post()

    with patch("arclasp.client._post", side_effect=mock_post):
        result = await governed.kickoff_async(inputs={"topic": "config"})
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert len(result) == 1
    assert any(c["path"] == "/v1/chains" for c in calls)
    assert count_event_calls(calls) == 2


# ===========================================================================
# Extra — Strategy B fallback (monkey-patch execute_task)
# ===========================================================================

@pytest.mark.asyncio
async def test_strategy_b_fallback_crewai():
    """
    Force Strategy B by using a crew with no before_task_callback / task_callback.
    The adapter monkey-patches agent.execute_task.  _StubCrewB dispatches into a
    worker thread via asyncio.to_thread so _fire() blocks the worker thread while
    the governance coroutine runs synchronously on the main event loop.
    The original method is restored in the finally block.
    """
    crew = _StubCrewB()
    governed = govern(crew, chain_name="crew-b")
    mock_post, calls = make_mock_post()

    with patch("arclasp.client._post", side_effect=mock_post):
        # _StubCrewB dispatches via asyncio.to_thread; the patched execute_task
        # fires governance coroutines synchronously on the main event loop via
        # run_coroutine_threadsafe + fut.result().
        result = await governed.kickoff_async(inputs={"topic": "data"})

        # Events are recorded synchronously; sleep is no longer required but
        # kept for compatibility with any remaining fire-and-forget paths.
        await asyncio.sleep(0.1)

    assert result == ["Result: Analyse the data"]

    # Cleanup: the patched closure is replaced by the original bound method.
    # We can't use `is` on bound methods (new object each access), but we can
    # verify the underlying function matches the class definition, which the
    # patched closure (a plain function) would not satisfy.
    restored = crew.agents[0].execute_task
    assert hasattr(restored, "__func__"), (
        "execute_task should be restored to a bound method, not the patched closure"
    )
    assert restored.__func__ is _MockAgent.execute_task

    # Governance events recorded on the background loop
    assert_event_recorded(calls, action_type="task_execution")
    assert_event_recorded(calls, action_type="task_result")


# ===========================================================================
# Regression — B-6: mixed callback scenario (CrewAI 1.x default)
# ===========================================================================

@pytest.mark.asyncio
async def test_mixed_callback_no_double_firing():
    """
    Regression for B-6: a crew with task_callback but no before_task_callback
    (the default shape of every CrewAI 1.x Crew instance) must emit exactly
    1× task_execution + 1× task_result per task — never 1× + 2×.

    Before the fix, _make_patched_execute_task always fired on_task_end even
    when Strategy A's task_callback wrapper was already active, producing a
    duplicate task_result event for every completed task.

    Event-ordering check: within each task's pair of events, task_execution
    must arrive before task_result (Strategy B fires start, Strategy A fires
    end — ordering must not be accidentally swapped by a future refactor).
    """
    crew = _StubCrewMixed()
    governed = govern(crew, chain_name="crew-mixed-b6")
    mock_post, calls = make_mock_post()

    with patch("arclasp.client._post", side_effect=mock_post):
        result = await governed.kickoff_async(inputs={"topic": "test"})
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert len(result) == 2
    assert_chain_completed(calls)

    event_calls = [c for c in calls if "events" in c["path"]]
    starts = [c for c in event_calls if c["body"].get("action_type") == "task_execution"]
    ends   = [c for c in event_calls if c["body"].get("action_type") == "task_result"]

    # 2 tasks × 1 start each (Strategy B) — B-6 would not affect this count
    assert len(starts) == 2, (
        f"Expected 2 task_execution events, got {len(starts)}: "
        f"{[c['body'] for c in starts]}"
    )
    # 2 tasks × 1 end each (Strategy A) — B-6 would produce 4 here
    assert len(ends) == 2, (
        f"Expected 2 task_result events, got {len(ends)}: "
        f"{[c['body'] for c in ends]}"
    )
    # Total: exactly 4 events for 2 tasks, not 6
    assert count_event_calls(calls) == 4

    # Ordering: every task_execution must appear before the next task_result
    # in the calls list.  Confirms Strategy B fires start before Strategy A
    # fires end — a regression guard against accidental event-order reversal.
    start_indices = [event_calls.index(c) for c in starts]
    end_indices   = [event_calls.index(c) for c in ends]
    for i, (s_idx, e_idx) in enumerate(zip(start_indices, end_indices)):
        assert s_idx < e_idx, (
            f"Task {i}: task_execution (index {s_idx}) must precede "
            f"task_result (index {e_idx}) in event stream"
        )


# ===========================================================================
# Regression — CR-01: parent_agent_name population
# ===========================================================================

@pytest.mark.asyncio
async def test_parent_agent_name_strategy_b():
    """
    Regression for CR-01 — Strategy B (pure monkey-patch) path.

    When execute_task is patched directly (no native task_callback), both
    on_task_start and on_task_end fire through Strategy B's wrapper.  Each
    event must carry:
      - agent_name  = task description (the governed entity / child)
      - parent_agent_name = agent.role (the executor / parent)

    Verifies the Crew → Agent → Task hierarchy is preserved in the audit
    trail for the pure Strategy B path (crew with no callback attributes).
    """
    crew = _StubCrewB()   # no task_callback / before_task_callback → pure Strategy B
    governed = govern(crew, chain_name="crew-b-cr01")
    mock_post, calls = make_mock_post()

    with patch("arclasp.client._post", side_effect=mock_post):
        await governed.kickoff_async(inputs={"topic": "data"})
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    event_calls = [c for c in calls if "events" in c["path"]]
    assert event_calls, "Expected at least one governance event"

    for ev in event_calls:
        body = ev["body"]
        # agent_name must be the task description, not the agent role
        assert body.get("agent_name") == "Analyse the data", (
            f"Expected agent_name='Analyse the data' (task label), "
            f"got {body.get('agent_name')!r}"
        )
        # parent_agent_name must be the agent's role
        assert body.get("parent_agent_name") == "Analyst", (
            f"Expected parent_agent_name='Analyst' (agent role), "
            f"got {body.get('parent_agent_name')!r}"
        )


@pytest.mark.asyncio
async def test_parent_agent_name_strategy_a_mixed():
    """
    Regression for CR-01 — Strategy A mixed path (CrewAI 1.x default).

    When the crew has task_callback but no before_task_callback, Strategy B
    provides on_task_start (execute_task patch) and Strategy A provides
    on_task_end_from_output (task_callback wrapper).  Both paths must set:
      - agent_name        = task description (the governed entity / child)
      - parent_agent_name = agent.role (the executor / parent)

    Uses _StubCrewMixed which accurately simulates the CrewAI 1.x Crew shape.
    Two agents ("Researcher", "Writer") run two tasks so we can assert the
    correct per-agent parent_agent_name on each event.
    """
    crew = _StubCrewMixed()   # task_callback only → mixed Strategy A + B
    governed = govern(crew, chain_name="crew-mixed-cr01")
    mock_post, calls = make_mock_post()

    with patch("arclasp.client._post", side_effect=mock_post):
        await governed.kickoff_async(inputs={"topic": "test"})
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    event_calls = [c for c in calls if "events" in c["path"]]
    assert len(event_calls) == 4, (
        f"Expected 4 events (2 tasks × start + end), got {len(event_calls)}"
    )

    # Build expected (agent_name, parent_agent_name) pairs — task description
    # and corresponding agent role, in event-stream order:
    # task_execution "Research the market" fired by Strategy B (Researcher)
    # task_result    "Research the market" fired by Strategy A (Researcher)
    # task_execution "Write the report"    fired by Strategy B (Writer)
    # task_result    "Write the report"    fired by Strategy A (Writer)
    expected_pairs = [
        ("Research the market", "Researcher"),
        ("Research the market", "Researcher"),
        ("Write the report",    "Writer"),
        ("Write the report",    "Writer"),
    ]

    for ev, (exp_agent, exp_parent) in zip(event_calls, expected_pairs):
        body = ev["body"]
        assert body.get("agent_name") == exp_agent, (
            f"agent_name: expected {exp_agent!r}, got {body.get('agent_name')!r}"
        )
        assert body.get("parent_agent_name") == exp_parent, (
            f"parent_agent_name: expected {exp_parent!r}, "
            f"got {body.get('parent_agent_name')!r}"
        )


# ===========================================================================
# Regression — Phase 3 fix: deny propagates through Strategy B execute_task
# ===========================================================================

@pytest.mark.asyncio
async def test_deny_propagates_through_strategy_b_execute_task():
    """
    Regression for fire-and-forget fix (Phase 3).

    In the mixed Strategy A+B path (CrewAI 1.x default), Strategy B's patched
    execute_task fires on_task_start.  _fire() now blocks via fut.result() so
    ActionDeniedError propagates back through the worker thread and halts the
    crew immediately.

    Before the fix: deny was swallowed; crew always completed.
    After the fix:  deny raises ActionDeniedError from kickoff_async.
    """
    tasks = [_MockTask("Analyse the data")]
    crew = _StubCrewMixed(tasks=tasks)
    governed = govern(crew, chain_name="crew-deny-b-regression")
    mock_post, calls = make_mock_post(deny_on="Analyse the data")

    with patch("arclasp.client._post", side_effect=mock_post):
        with pytest.raises(ActionDeniedError):
            await governed.kickoff_async(inputs={"topic": "test"})

    # The task_execution event was sent (and was the one denied)
    assert_event_recorded(calls, action_name="Analyse the data", action_type="task_execution")
    # No task_result — task was denied before completion
    task_results = [
        c for c in calls
        if "events" in c["path"]
        and c["body"].get("action_type") == "task_result"
    ]
    assert len(task_results) == 0, (
        f"Expected no task_result after deny, got: {[c['body'] for c in task_results]}"
    )


# ===========================================================================
# Regression — BUG-CR-02: object.__setattr__ bypasses Pydantic v2 validation
# ===========================================================================

def test_bug_cr02_object_setattr_bypasses_pydantic():
    """
    Regression for BUG-CR-02 (simulated).

    crewai.Agent inherits pydantic.BaseModel.  In Pydantic v2, direct attribute
    assignment raises ValueError when the attribute is not a declared model field.
    execute_task is a method (not a field), so patching it requires
    object.__setattr__ to bypass Pydantic's __setattr__ validation.

    This test uses a minimal Pydantic v2 model to verify the mechanism without
    requiring the real crewai package.
    """
    from pydantic import BaseModel

    class PydanticAgent(BaseModel):
        role: str

        def execute_task(self, task: Any) -> str:
            return f"original: {task}"

    agent = PydanticAgent(role="tester")

    # Direct assignment raises on Pydantic v2 BaseModel — confirming the bug
    with pytest.raises((ValueError, TypeError)):
        agent.execute_task = lambda task: "patched"  # type: ignore[method-assign]

    # object.__setattr__ bypasses Pydantic's validation — this is the fix
    def patched_fn(task):
        return "patched"

    object.__setattr__(agent, "execute_task", patched_fn)
    assert agent.execute_task("x") == "patched"

    # Restore also works via object.__setattr__ — this is _restore_instrumentation()'s path
    original_bound = PydanticAgent.execute_task.__get__(agent)
    object.__setattr__(agent, "execute_task", original_bound)
    assert agent.execute_task("x") == "original: x"


# ---------------------------------------------------------------------------
# Helper: detect whether real crewai (not the conftest stub) is importable
# ---------------------------------------------------------------------------

def _is_real_crewai_installed() -> bool:
    """True when the real crewai package is installed in this Python environment."""
    import importlib.util
    try:
        spec = importlib.util.find_spec("crewai")
    except ValueError:
        # Stub module injected by conftest has __spec__ = None; find_spec raises
        # ValueError in that case.  Real crewai is not installed here.
        return False
    # The stub module is a ModuleType with no origin; real crewai has one.
    return spec is not None and spec.origin is not None


_REAL_CREWAI_AVAILABLE = _is_real_crewai_installed()


@pytest.mark.skipif(not _REAL_CREWAI_AVAILABLE, reason="real crewai package not installed")
def test_bug_cr02_against_real_crewai_agent():
    """
    Regression for BUG-CR-02 (high-fidelity — real crewai.Agent).

    Verifies that object.__setattr__ correctly patches the execute_task method
    on a real crewai.Agent (a Pydantic v2 BaseModel subclass), and that
    _restore_instrumentation() can restore the original method.

    Skipped when crewai is not installed in the current Python environment
    (SDK test environment uses a sys.modules stub instead).
    """
    import sys
    import importlib

    # Temporarily remove the stub so we can import the real package.
    _stub = sys.modules.pop("crewai", None)
    try:
        real_crewai = importlib.import_module("crewai")
        _Agent = getattr(real_crewai, "Agent", None)
        if _Agent is None:
            pytest.skip("crewai.Agent not found in real package")
    except ImportError:
        pytest.skip("crewai not importable in this environment")
    finally:
        # Always restore the stub so other tests are unaffected.
        if _stub is not None:
            sys.modules["crewai"] = _stub

    # _Agent is now the real crewai.Agent class (captured before stub restore).
    try:
        agent = _Agent(
            role="tester",
            goal="verify BUG-CR-02 fix",
            backstory="Test agent for patching regression",
            verbose=False,
        )
    except Exception as exc:
        pytest.skip(f"Could not instantiate crewai.Agent (LLM not configured?): {exc}")

    # Direct assignment must raise on a real Pydantic v2 Agent
    with pytest.raises((ValueError, TypeError)):
        agent.execute_task = lambda task: "patched"  # type: ignore[method-assign]

    # object.__setattr__ must succeed — this is what _patch_all_agents() now uses
    object.__setattr__(agent, "execute_task", lambda task: "patched")
    assert agent.execute_task("x") == "patched"
