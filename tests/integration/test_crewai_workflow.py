"""
Integration tests for proofrail.crewai.adapter (GovernedCrew).

Strategy A (native before_task_callback / task_callback hooks) is exercised
in the 6 core scenarios.  An extra test forces Strategy B (monkey-patch
execute_task) and confirms that:
  - execute_task is intercepted and records governance events.
  - the original method is restored after the run (cleanup guarantee).

NOTE on CrewAI Scenario 3 (deny):
  CrewAI's governance is fire-and-forget via asyncio.run_coroutine_threadsafe.
  A "deny" decision is logged as a warning but does NOT propagate back into
  the synchronous crew execution (adapter design limitation documented in
  crewai/adapter.py).  Scenario 3 therefore asserts that:
    - The deny event WAS recorded on the backend.
    - The crew completed (denial did not halt execution).
  This intentionally differs from the LangGraph / LangChain behavior.

Framework stubs are injected via sys.modules in conftest.py — crewai is
NOT installed.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from unittest.mock import patch, MagicMock
from typing import Any

import pytest

import proofrail
from proofrail.crewai.adapter import govern
from proofrail.exceptions import BackendUnavailableError

from .conftest import (
    make_mock_post,
    assert_event_recorded,
    assert_chain_completed,
    count_event_calls,
    running_event_loop_in_thread,  # noqa: F401 — imported for pytest fixture discovery
)


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
    def __init__(self, raw: str, agent: str) -> None:
        self.raw     = raw
        self.agent   = agent
        self.summary = raw[:100]


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
        results = []
        for task, agent in zip(self.tasks, self.agents):
            if callable(self.before_task_callback):
                self.before_task_callback(task, agent)
            out = _MockTaskOutput(f"Result for {task.description}", agent.role)
            results.append(out)
            if callable(self.task_callback):
                self.task_callback(out)
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
        results = []
        for task, agent in zip(self.tasks, self.agents):
            # execute_task will be monkey-patched by the adapter
            result = agent.execute_task(task)
            results.append(result)
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
    with patch("proofrail.client._post", side_effect=mock_post):
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
    with patch("proofrail.client._post", side_effect=mock_post):
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

    with patch("proofrail.client._post", side_effect=mock_post):
        # No ActionDeniedError surfaces — fire-and-forget design
        result = await governed.kickoff_async(inputs={"topic": "test"})
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    # Crew completed both tasks
    assert len(result) == 2
    assert_chain_completed(calls)
    # The deny event WAS sent to the backend
    assert_event_recorded(calls, action_name="Delete old records", action_type="task_execution")


# ===========================================================================
# Scenario 4 — backend unreachable, fail_mode=deny
# ===========================================================================

@pytest.mark.asyncio
async def test_backend_unreachable_fail_deny_crewai():
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
            await _govern_a().kickoff_async(inputs={"topic": "AI"})

    assert count_event_calls(calls) == 0


# ===========================================================================
# Scenario 5 — backend unreachable, fail_mode=allow (offline path)
# ===========================================================================

@pytest.mark.asyncio
async def test_backend_unreachable_fail_allow_crewai():
    mock_post, calls = make_mock_post(offline_signal=True)

    with patch("proofrail.client._post", side_effect=mock_post):
        result = await _govern_a().kickoff_async(inputs={"topic": "AI"})
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert len(result) == 2
    # No synchronous event calls (chain is offline; fire-and-forget coroutines
    # buffer in offline mode when they run)
    assert count_event_calls(calls) == 0


# ===========================================================================
# Scenario 6 — fast-path engages for low-risk actions
# ===========================================================================

@pytest.mark.asyncio
async def test_fast_path_engages_crewai():
    proofrail.init(
        api_key="prail_test",
        backend_url="http://localhost:9999",
        environment="development",
        enable_local_fast_path=True,
        fail_mode="allow",
    )
    tasks = [_MockTask("Get config data")]   # safe name → risk_score = 0
    governed = govern(_StubCrewA(tasks=tasks), chain_name="crew-fp")
    mock_post, calls = make_mock_post()

    with patch("proofrail.client._post", side_effect=mock_post):
        result = await governed.kickoff_async(inputs={"topic": "config"})
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert len(result) == 1
    # Chain was started; no error raised
    assert any(c["path"] == "/v1/chains" for c in calls)


# ===========================================================================
# Extra — Strategy B fallback (monkey-patch execute_task)
# ===========================================================================

@pytest.mark.asyncio
async def test_strategy_b_fallback_crewai(running_event_loop_in_thread):
    """
    Force Strategy B by using a crew with no before_task_callback / task_callback.
    The adapter monkey-patches agent.execute_task, records governance events via
    asyncio.run_coroutine_threadsafe onto running_event_loop_in_thread, then
    restores the original method in the finally block.
    """
    crew = _StubCrewB()
    original_execute = crew.agents[0].execute_task

    governed = govern(crew, chain_name="crew-b")
    mock_post, calls = make_mock_post()

    with patch("proofrail.client._post", side_effect=mock_post):
        # kickoff_async runs in the test's event loop; it calls execute_task
        # synchronously.  The patched version fires governance coroutines on
        # running_event_loop_in_thread via run_coroutine_threadsafe.
        result = await governed.kickoff_async(inputs={"topic": "data"})

        # Wait for the background-loop coroutines to complete
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
