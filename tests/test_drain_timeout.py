"""
sdk/tests/test_drain_timeout.py
================================
Regression tests for BUG-LR-01 — _complete() drain timeout formula.

Three surfaces:
  1. Formula correctness: timeout scales with buffer size so slow backends
     don't drop events they could have drained within the extended window.
  2. Drop count logging: actual_dropped reflects events still in the buffer
     at the moment of failure, not the worst-case entry-time count.
  3. Config override: drain_timeout_seconds bypasses the formula entirely.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, patch

import pytest

import proofrail
from proofrail.chain import Chain


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _chain_start_response(chain_id: str = "test-chain-id") -> dict:
    return {"id": chain_id, "status": "active"}


def _allow_response() -> dict:
    return {
        "policy_decision": "allow",
        "decision_reason": "",
        "decision_source": "backend_evaluation",
    }


def _init(backend_timeout_seconds: int = 2, drain_timeout_seconds: int | None = None) -> None:
    kw = dict(
        api_key="prail_test",
        backend_url="http://test.invalid",
        environment="development",
        enable_local_fast_path=True,
        fail_mode="deny",
        backend_timeout_seconds=backend_timeout_seconds,
    )
    if drain_timeout_seconds is not None:
        kw["drain_timeout_seconds"] = drain_timeout_seconds
    proofrail.init(**kw)


# ---------------------------------------------------------------------------
# Test 1 — formula ensures all events drain when backend is slow
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_drain_timeout_scales_with_buffer_size():
    """
    With 5 buffered fast-path events and a mock _post that costs 2 s each,
    the old hardcoded 10 s timeout would cancel after event 4 (10 s / 2 s = 5,
    right at the edge — event 5 gets dropped).  The formula:

        max(10.0, 5 * 2 * 1.5) = max(10.0, 15.0) = 15 s

    gives 50% headroom, so all 5 events drain before _complete() is called.
    """
    _init(backend_timeout_seconds=2)

    call_log: list[str] = []

    async def slow_post(path: str, body: dict, action_type: str | None = None) -> dict:
        call_log.append(path)
        if path == "/v1/chains":
            return _chain_start_response()
        if "events" in path:
            await asyncio.sleep(2)   # 2 s per event — same as backend_timeout_seconds
        return _allow_response()

    with patch("proofrail.client._post", side_effect=slow_post):
        async with Chain("lr01-scale-test") as chain:
            for i in range(5):
                await chain.record_agent_action(
                    agent_name="agent",
                    action_type="tool_call",
                    action_name=f"action_{i}",
                    payload={},
                )
            # All 5 events are now in _offline_buffer.
            # Confirm formula: max(10.0, 5 * 2 * 1.5) = 15 s
            from proofrail import client as _pc
            config = _pc.get_config()
            buf = len(chain._offline_buffer)
            expected_timeout = max(10.0, buf * config.backend_timeout_seconds * 1.5)
            assert expected_timeout == 15.0, (
                f"Formula produced {expected_timeout}, expected 15.0"
            )
        # __aexit__ ran _complete() with the 15 s window.
        # 5 events × 2 s = 10 s < 15 s → all events drained.

    event_calls = [p for p in call_log if "events" in p]
    assert len(event_calls) == 5, (
        f"Expected 5 event POST calls, got {len(event_calls)}. "
        "Drain timeout too short — some events were not sent."
    )


# ---------------------------------------------------------------------------
# Test 2 — drop count logged is actual remainder, not entry-time worst case
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_drain_timeout_drop_count_logged(caplog):
    """
    Mock drains the first 2 of 5 events successfully, then stalls forever.
    _complete() should:
      - log a WARNING containing "3 event(s) dropped" (not "5 event(s) dropped")
      - include the timeout value in the message
    The floor timeout of 10.0 s applies (buffer_size=5, backend_timeout=1,
    formula = max(10.0, 5*1*1.5=7.5) = 10.0 s).
    """
    _init(backend_timeout_seconds=1)

    drain_stall = asyncio.Event()   # never set → mock hangs after 2 events
    events_sent = 0

    async def partial_post(path: str, body: dict, action_type: str | None = None) -> dict:
        nonlocal events_sent
        if path == "/v1/chains":
            return _chain_start_response()
        if "events" in path:
            if events_sent < 2:
                events_sent += 1
                return _allow_response()
            # Stall indefinitely on events 3-5.
            await drain_stall.wait()
        return _allow_response()

    with caplog.at_level(logging.WARNING, logger="proofrail.chain"):
        with patch("proofrail.client._post", side_effect=partial_post):
            async with Chain("lr01-drop-count-test") as chain:
                for i in range(5):
                    await chain.record_agent_action(
                        agent_name="agent",
                        action_type="tool_call",
                        action_name=f"action_{i}",
                        payload={},
                    )
            # _complete() runs: drain sends 2 events, stalls, timeout fires.

    warning_records = [
        r for r in caplog.records
        if r.levelname == "WARNING" and r.name == "proofrail.chain"
    ]
    assert warning_records, "Expected a WARNING log from the drain timeout"

    msg = warning_records[0].message
    assert "3 event(s) dropped" in msg, (
        f"Expected '3 event(s) dropped' in WARNING message, got: {msg!r}"
    )
    assert "timeout=10.0s" in msg, (
        f"Expected 'timeout=10.0s' in WARNING message, got: {msg!r}"
    )


# ---------------------------------------------------------------------------
# Test 3 — drain_timeout_seconds config override bypasses formula
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_drain_timeout_config_override():
    """
    When drain_timeout_seconds=60 is set, _complete() uses 60.0 s exactly —
    not the formula value (which for 3 events at backend_timeout=2 would be
    max(10.0, 3*2*1.5=9.0) = 10.0 s).
    """
    _init(backend_timeout_seconds=2, drain_timeout_seconds=60)

    wait_for_calls: list[float] = []
    original_wait_for = asyncio.wait_for

    async def spy_wait_for(coro, timeout=None, **kw):
        wait_for_calls.append(timeout)
        return await original_wait_for(coro, timeout=timeout, **kw)

    async def fast_post(path: str, body: dict, action_type: str | None = None) -> dict:
        if path == "/v1/chains":
            return _chain_start_response()
        return _allow_response()

    with patch("proofrail.client._post", side_effect=fast_post):
        with patch("proofrail.chain.asyncio.wait_for", side_effect=spy_wait_for):
            async with Chain("lr01-override-test") as chain:
                for i in range(3):
                    await chain.record_agent_action(
                        agent_name="agent",
                        action_type="tool_call",
                        action_name=f"action_{i}",
                        payload={},
                    )
            # _complete() called here.

    # wait_for should have been called with 60.0, not 10.0 (formula floor).
    assert wait_for_calls, "asyncio.wait_for was never called — drain task missing?"
    assert 60.0 in wait_for_calls, (
        f"Expected wait_for called with 60.0 (config override), got: {wait_for_calls}"
    )
    assert 10.0 not in wait_for_calls, (
        f"Formula floor 10.0 was used instead of config override 60.0: {wait_for_calls}"
    )
