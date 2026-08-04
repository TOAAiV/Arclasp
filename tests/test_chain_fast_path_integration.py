"""
Regression tests for K1 backend-authoritative Chain.record_agent_action behavior.

The legacy local fast path remains as a compatibility module, but public governed
Chain execution must always require an authoritative backend decision before an
action is allowed to proceed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import proofrail
from proofrail.chain import Chain
from proofrail.exceptions import ActionDeniedError, BackendUnavailableError


@pytest.fixture(autouse=True)
def sdk_dev():
    proofrail.init(
        api_key="prail_test",
        backend_url="http://localhost:9999",
        environment="development",
        enable_local_fast_path=False,
        fail_mode="deny",
        default_approval_timeout_hours=0,
    )


def _chain_start_response(chain_id: str = "chain-k1-001"):
    return {"id": chain_id}


def _allow_response():
    return {
        "policy_decision": "allow",
        "decision_reason": "Action permitted by policy",
        "decision_source": "backend_evaluation",
    }


def _deny_response():
    return {
        "policy_decision": "deny",
        "decision_reason": "IAM/permission modification blocked",
        "decision_source": "backend_evaluation",
        "policy_name": "cumulative_financial_threshold",
    }


def _approval_required_response():
    return {
        "policy_decision": "require_approval",
        "decision_reason": "Human approval required",
        "decision_source": "backend_evaluation",
        "policy_name": "cumulative_financial_threshold",
    }


@pytest.mark.asyncio
async def test_development_default_safe_action_calls_backend_event():
    event_posts = []

    async def mock_post(path, body, action_type=None):
        if path == "/v1/chains":
            return _chain_start_response()
        if "/events" in path:
            event_posts.append((path, body, action_type))
            return _allow_response()
        return {}

    with patch("proofrail.client._post", side_effect=mock_post):
        async with Chain("k1-default") as chain:
            result = await chain.record_agent_action(
                agent_name="agent",
                action_type="tool_call",
                action_name="get_data",
                payload={},
            )

    assert result.policy_decision == "allow"
    assert result.decision_source == "backend_evaluation"
    assert len(event_posts) == 1
    assert event_posts[0][2] == "tool_call"
    assert event_posts[0][1]["idempotency_key"]


@pytest.mark.asyncio
async def test_explicit_enable_local_fast_path_warns_but_still_calls_backend():
    with pytest.warns(DeprecationWarning, match="enable_local_fast_path"):
        proofrail.init(
            api_key="prail_test",
            backend_url="http://localhost:9999",
            environment="development",
            enable_local_fast_path=True,
            fail_mode="deny",
        )

    calls = []

    async def mock_post(path, body, action_type=None):
        calls.append({"path": path, "body": dict(body or {})})
        if path == "/v1/chains":
            return _chain_start_response("chain-k1-explicit")
        if "/events" in path:
            return _allow_response()
        return {}

    with patch("proofrail.client._post", side_effect=mock_post):
        async with Chain("k1-explicit-fast-path") as chain:
            result = await chain.record_agent_action(
                agent_name="agent",
                action_type="tool_call",
                action_name="get_config",
                payload={},
            )

    assert result.decision_source == "backend_evaluation"
    assert [c["path"] for c in calls].count("/v1/chains") == 1
    assert len([c for c in calls if "/events" in c["path"]]) == 1
    assert result.decision_source != "local_fast_path"


@pytest.mark.asyncio
async def test_backend_down_with_fast_path_config_fails_closed_before_action():
    with pytest.warns(DeprecationWarning):
        proofrail.init(
            api_key="prail_test",
            backend_url="http://localhost:9999",
            environment="development",
            enable_local_fast_path=True,
            fail_mode="allow",
        )

    async def mock_post(path, body, action_type=None):
        if path == "/v1/chains":
            return _chain_start_response("chain-k1-down")
        raise BackendUnavailableError("backend down", fail_mode="allow")

    with patch("proofrail.client._post", side_effect=mock_post):
        async with Chain("k1-down") as chain:
            with pytest.raises(BackendUnavailableError) as exc_info:
                await chain.record_agent_action(
                    agent_name="agent",
                    action_type="tool_call",
                    action_name="get_config",
                    payload={},
                )

    assert exc_info.value.fail_mode == "allow"
    assert chain._offline is False
    assert chain._offline_buffer == []


@pytest.mark.asyncio
async def test_backend_deny_is_returned_as_action_denied():
    async def mock_post(path, body, action_type=None):
        if path == "/v1/chains":
            return _chain_start_response()
        if "/events" in path:
            return _deny_response()
        return {}

    with patch("proofrail.client._post", side_effect=mock_post):
        async with Chain("k1-deny") as chain:
            with pytest.raises(ActionDeniedError) as exc_info:
                await chain.record_agent_action(
                    agent_name="agent",
                    action_type="tool_call",
                    action_name="update_iam_policy",
                    payload={},
                )

    assert exc_info.value.decision_source == "backend_evaluation"


@pytest.mark.asyncio
async def test_backend_require_approval_returns_human_approval_after_poll():
    async def mock_post(path, body, action_type=None):
        if path == "/v1/chains":
            return _chain_start_response("chain-k1-approval")
        if "/events" in path:
            return _approval_required_response()
        return {}

    with patch("proofrail.client._post", side_effect=mock_post), patch.object(
        Chain, "_poll_for_approval", new=AsyncMock(return_value="looks good")
    ):
        async with Chain("k1-approval") as chain:
            result = await chain.record_agent_action(
                agent_name="agent",
                action_type="tool_call",
                action_name="charge_card",
                payload={"amount_usd": 9000},
            )

    assert result.policy_decision == "allow"
    assert result.decision_source == "human_approval"
    assert result.policy_name == "cumulative_financial_threshold"
    assert "looks good" in (result.decision_reason or "")


@pytest.mark.asyncio
async def test_no_fast_path_async_memory_buffer_is_used_for_public_execution():
    async def mock_post(path, body, action_type=None):
        if path == "/v1/chains":
            return _chain_start_response()
        if "/events" in path:
            return _allow_response()
        return {}

    with patch("proofrail.client._post", side_effect=mock_post):
        async with Chain("k1-no-buffer") as chain:
            result = await chain.record_agent_action(
                agent_name="agent",
                action_type="tool_call",
                action_name="get_data",
                payload={},
            )
            assert result.decision_source == "backend_evaluation"
            assert chain._offline_buffer == []
            assert chain._drain_task is None
