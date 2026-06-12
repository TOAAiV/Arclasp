"""
sdk/tests/test_fast_path_metrics.py
=====================================
Regression tests for SDK-S-2 — fast-path decisions must update
chain._cumulative_metrics so that criterion 4 of fast-path eligibility
(cumulative financial exposure >= 80% of threshold -> defer to backend)
works correctly across multiple events in the same chain.

Before the fix: evaluate_fast_path discarded the updated_cumulative_metrics
computed by process_action_local, and chain.py never wrote back to
self._cumulative_metrics.  Every fast-path eligibility check evaluated
against zero exposure regardless of how many events had been recorded.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

import proofrail
from proofrail import client as _proofrail_client
from proofrail.chain import Chain
from proofrail.fast_path import is_fast_path_eligible


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _chain_start_response(chain_id: str = "metrics-test-chain") -> dict:
    return {"id": chain_id, "status": "active"}


def _allow_response() -> dict:
    return {
        "policy_decision": "allow",
        "decision_reason": "",
        "decision_source": "backend_evaluation",
    }


def _init_fast_path(cumulative_threshold_usd: float = 10_000.0) -> None:
    proofrail.init(
        api_key="prail_test",
        backend_url="http://localhost:9999",
        environment="development",
        enable_local_fast_path=True,
        fail_mode="deny",
        cumulative_financial_threshold_usd=cumulative_threshold_usd,
    )


def _init_production() -> None:
    proofrail.init(
        api_key="prail_test",
        backend_url="http://localhost:9999",
        environment="production",
        enable_local_fast_path=True,
        fail_mode="deny",
    )


async def _mock_post(path: str, body: dict, action_type: str | None = None) -> dict:
    if path == "/v1/chains":
        return _chain_start_response()
    return _allow_response()


# ---------------------------------------------------------------------------
# Test 1: Sequential accumulation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fast_path_metrics_accumulate_sequentially():
    """
    Five sequential fast-path allow events each with amount=$100 must
    accumulate financial_exposure_usd correctly in chain._cumulative_metrics.

    Without the fix: _cumulative_metrics stays {} — every event evaluates
    against zero exposure.
    With the fix: exposure grows $100 per event, reaching $500 after 5 events.
    """
    _init_fast_path()

    with patch("proofrail.client._post", side_effect=_mock_post):
        async with Chain("metrics-sequential") as chain:
            for i in range(5):
                decision = await chain.record_agent_action(
                    agent_name="agent",
                    action_type="tool_call",
                    action_name="get_record",
                    payload={"amount": 100},
                )
                assert decision.decision_source == "local_fast_path", (
                    f"Event {i + 1} should be fast-path, got {decision.decision_source}"
                )

    assert chain._cumulative_metrics.get("financial_exposure_usd") == 500.0, (
        f"Expected financial_exposure_usd=500.0 after 5 events, "
        f"got {chain._cumulative_metrics}"
    )


# ---------------------------------------------------------------------------
# Test 2: Threshold transition
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fast_path_threshold_triggers_backend_fallback():
    """
    With cumulative_financial_threshold_usd=1000:
      - Events 1-8 each add $100 -> cumulative reaches $800 after event 8.
      - is_fast_path_eligible with those metrics: $800 >= 1000*0.8=800 -> False.

    Verifies that after the fix, the accumulated metrics actually reach $800
    and trigger the criterion-4 ineligibility gate.

    Without the fix: _cumulative_metrics stays {}, so criterion 4 always
    sees zero exposure and never blocks fast-path regardless of actual spend.
    """
    _init_fast_path(cumulative_threshold_usd=1_000.0)
    config = _proofrail_client.get_config()

    with patch("proofrail.client._post", side_effect=_mock_post):
        async with Chain("metrics-threshold") as chain:
            for i in range(8):
                decision = await chain.record_agent_action(
                    agent_name="agent",
                    action_type="tool_call",
                    action_name="get_record",
                    payload={"amount": 100},
                )
                assert decision.decision_source == "local_fast_path", (
                    f"Event {i + 1} should be fast-path, got {decision.decision_source}"
                )

            # After 8 events the local exposure snapshot must be $800.
            assert chain._cumulative_metrics.get("financial_exposure_usd") == 800.0, (
                f"Expected 800.0 after 8 events, got {chain._cumulative_metrics}"
            )

            # Event 9 would see $800 >= 80% of $1000 = $800 -> ineligible.
            eligible, reason = is_fast_path_eligible(
                action_type="tool_call",
                action_name="get_record",
                payload={"amount": 100},
                agent_name="agent",
                cumulative_metrics=chain._cumulative_metrics,
                config=config,
            )
            assert not eligible, (
                f"Expected fast-path ineligible at $800 exposure (80% of $1000), "
                f"got eligible (reason={reason!r})"
            )
            assert "80%" in reason, (
                f"Ineligibility reason should reference the 80% threshold: {reason!r}"
            )


# ---------------------------------------------------------------------------
# Test 3: Concurrency — no lost updates under asyncio.gather
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fast_path_metrics_accumulate_under_gather():
    """
    Ten concurrent fast-path allow events launched via asyncio.gather must all
    contribute to financial_exposure_usd — no update must be lost.

    No asyncio.Lock is needed because the fast-path branch in
    record_agent_action contains no await between reading and writing
    _cumulative_metrics.  In asyncio's cooperative model, tasks are never
    interleaved at purely synchronous code paths, so the read-modify-write
    is atomic from the scheduler's perspective.

    Without the fix: _cumulative_metrics stays {} — gather result = 0.
    With the fix: each task sees the previous task's update = $1000 total.
    """
    _init_fast_path()

    with patch("proofrail.client._post", side_effect=_mock_post):
        async with Chain("metrics-gather") as chain:
            decisions = await asyncio.gather(*[
                chain.record_agent_action(
                    agent_name="agent",
                    action_type="tool_call",
                    action_name=f"get_record_{i}",
                    payload={"amount": 100},
                )
                for i in range(10)
            ])

    # Every event must have been fast-path allowed.
    for i, decision in enumerate(decisions):
        assert decision.decision_source == "local_fast_path", (
            f"Event {i} expected local_fast_path, got {decision.decision_source}"
        )

    # All 10 contributions must be reflected — no lost updates.
    assert chain._cumulative_metrics.get("financial_exposure_usd") == 1000.0, (
        f"Expected financial_exposure_usd=1000.0 (10 x $100), "
        f"got {chain._cumulative_metrics}. "
        "A value < 1000.0 indicates lost updates."
    )


# ---------------------------------------------------------------------------
# Test 4: Backend-routed events must not touch _cumulative_metrics
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_backend_path_does_not_update_local_metrics():
    """
    Backend-routed events (production environment — fast-path criterion 2
    disables it) must leave chain._cumulative_metrics unchanged at {}.

    Regression guard: the SDK-S-2 fix touches only the fast-path branch.
    Backend events are governed by the backend's own DB-stored cumulative
    state; the local snapshot has no reliable mechanism to stay in sync with
    those values and must not be updated by the backend path.
    """
    _init_production()

    with patch("proofrail.client._post", side_effect=_mock_post):
        async with Chain("metrics-backend") as chain:
            for _ in range(3):
                decision = await chain.record_agent_action(
                    agent_name="agent",
                    action_type="tool_call",
                    action_name="get_record",
                    payload={"amount": 100},
                )
                assert decision.decision_source != "local_fast_path", (
                    "Production event must not use local_fast_path"
                )

    assert chain._cumulative_metrics == {}, (
        f"Backend-routed events must not update _cumulative_metrics, "
        f"got {chain._cumulative_metrics}"
    )
