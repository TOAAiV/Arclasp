"""
K1 compatibility tests for the legacy fast_path metrics helpers.

The compatibility module can still evaluate local metrics when called directly,
but public Chain execution no longer uses it as authority.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import proofrail
from proofrail import client as _proofrail_client
from proofrail.chain import Chain
from proofrail.fast_path import evaluate_fast_path, is_fast_path_eligible


def _chain_start_response(chain_id: str = "metrics-test-chain") -> dict:
    return {"id": chain_id, "status": "active"}


def _allow_response() -> dict:
    return {
        "policy_decision": "allow",
        "decision_reason": "",
        "decision_source": "backend_evaluation",
    }


def _init_deprecated_fast_path(cumulative_threshold_usd: float = 10_000.0) -> None:
    with pytest.warns(DeprecationWarning, match="enable_local_fast_path"):
        proofrail.init(
            api_key="prail_test",
            backend_url="http://localhost:9999",
            environment="development",
            enable_local_fast_path=True,
            fail_mode="deny",
            cumulative_financial_threshold_usd=cumulative_threshold_usd,
        )


async def _mock_post(path: str, body: dict, action_type: str | None = None) -> dict:
    if path == "/v1/chains":
        return _chain_start_response()
    return _allow_response()


def test_legacy_fast_path_evaluator_still_updates_direct_metrics():
    _init_deprecated_fast_path()
    config = _proofrail_client.get_config()

    result = evaluate_fast_path(
        "tool_call",
        "get_record",
        {"amount": 100},
        "agent",
        {},
        config,
    )

    assert result is not None
    assert result["decision_source"] == "local_fast_path"
    assert result["updated_cumulative_metrics"]["financial_exposure_usd"] == 100.0


def test_legacy_fast_path_evaluator_threshold_still_blocks_direct_use():
    _init_deprecated_fast_path(cumulative_threshold_usd=1_000.0)
    config = _proofrail_client.get_config()

    eligible, reason = is_fast_path_eligible(
        action_type="tool_call",
        action_name="get_record",
        payload={"amount": 100},
        agent_name="agent",
        cumulative_metrics={"financial_exposure_usd": 800.0},
        config=config,
    )

    assert not eligible
    assert "80%" in reason


@pytest.mark.asyncio
async def test_public_chain_does_not_update_local_fast_path_metrics():
    _init_deprecated_fast_path()
    event_bodies: list[dict] = []

    async def mock_post(path: str, body: dict, action_type: str | None = None) -> dict:
        if path == "/v1/chains":
            return _chain_start_response()
        if "/events" in path:
            event_bodies.append(dict(body))
        return _allow_response()

    with patch("proofrail.client._post", side_effect=mock_post):
        async with Chain("metrics-public") as chain:
            for _ in range(3):
                decision = await chain.record_agent_action(
                    agent_name="agent",
                    action_type="tool_call",
                    action_name="get_record",
                    payload={"amount": 100},
                )
                assert decision.decision_source == "backend_evaluation"

    assert len(event_bodies) == 3
    assert chain._cumulative_metrics == {}
    assert chain._offline_buffer == []
