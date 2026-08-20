"""
Tests for the approval-resolution code path.

Verifies that record_agent_action returns a fully-resolved PolicyDecision
(policy_decision="allow", decision_source="human_approval") after a human
approver grants the action, and raises ActionDeniedError with the correct
fields when an approver denies it or the approval times out.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import arclasp
from arclasp.chain import Chain
from arclasp.exceptions import ActionDeniedError
from arclasp.models import PolicyDecision


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_chain_start_response(chain_id: str = "chain-approval-001") -> dict:
    return {"id": chain_id}


def _make_require_approval_response(policy_name: str = "cumulative_financial_threshold") -> dict:
    return {
        "policy_decision": "require_approval",
        "decision_reason": "Amount exceeds threshold — awaiting human approval",
        "decision_source": "backend_evaluation",
        "policy_name": policy_name,
        "kill_switch_active": False,
        "pause_reason": None,
        "remediation": None,
        "docs_url": None,
    }


def _make_approval_status_response(
    approval_status: str,
    reason: str | None = None,
    decision_notes: str | None = None,
) -> dict:
    approvals = []
    if approval_status in ("approved", "denied"):
        approvals = [{"reason": reason, "decision_notes": decision_notes, "status": approval_status}]
    return {
        "chain_id": "chain-approval-001",
        "chain_status": "pending_approval",
        "approval_status": approval_status,
        "approvals": approvals,
    }


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def sdk_init():
    arclasp.init(
        api_key="prail_test",
        backend_url="http://localhost:9999",
        default_approval_timeout_hours=1,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestApprovalResolution:
    @pytest.mark.asyncio
    async def test_approved_with_notes_returns_resolved_decision(self):
        """
        When a human approves with notes, record_agent_action must return a
        PolicyDecision(policy_decision="allow", decision_source="human_approval")
        with the approver's notes in decision_reason.
        """
        require_resp = _make_require_approval_response()
        approved_resp = _make_approval_status_response(
            "approved",
            reason="policy-required-approval",
            decision_notes="Looks good, approved.",
        )

        post_call_count = 0

        async def fake_post(path, data, action_type=None):
            nonlocal post_call_count
            post_call_count += 1
            if path == "/v1/chains":
                return _make_chain_start_response()
            if path.endswith("/complete"):
                return {"id": "chain-approval-001", "status": "completed"}
            return require_resp

        async def fake_get(path):
            return approved_resp

        with patch("arclasp.client._post", side_effect=fake_post), \
             patch("arclasp.client._get", side_effect=fake_get), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            async with Chain("test-approval") as chain:
                decision = await chain.record_agent_action(
                    agent_name="finance-agent",
                    action_type="tool_call",
                    action_name="wire_transfer",
                    payload={"amount_usd": 6000},
                )

        assert isinstance(decision, PolicyDecision)
        assert decision.policy_decision == "allow"
        assert decision.decision_source == "human_approval"
        assert "Approved by human reviewer" in decision.decision_reason
        assert "Looks good, approved." in decision.decision_reason
        assert "policy-required-approval" not in decision.decision_reason

    @pytest.mark.asyncio
    async def test_approved_without_notes_returns_resolved_decision(self):
        """
        When a human approves without leaving notes, decision_reason must be
        the generic "Approved by human reviewer" string.
        """
        require_resp = _make_require_approval_response()
        approved_resp = _make_approval_status_response("approved", reason=None)

        async def fake_post(path, data, action_type=None):
            if path == "/v1/chains":
                return _make_chain_start_response()
            if path.endswith("/complete"):
                return {"id": "chain-approval-001", "status": "completed"}
            return require_resp

        async def fake_get(path):
            return approved_resp

        with patch("arclasp.client._post", side_effect=fake_post), \
             patch("arclasp.client._get", side_effect=fake_get), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            async with Chain("test-approval-no-notes") as chain:
                decision = await chain.record_agent_action(
                    agent_name="finance-agent",
                    action_type="tool_call",
                    action_name="wire_transfer",
                    payload={"amount_usd": 6000},
                )

        assert decision.policy_decision == "allow"
        assert decision.decision_source == "human_approval"
        assert decision.decision_reason == "Approved by human reviewer"

    @pytest.mark.asyncio
    async def test_denied_raises_action_denied_error(self):
        """
        When a human denies the action, _poll_for_approval must raise
        ActionDeniedError with policy_name="human_approval_denied" and
        decision_source="human_approval".
        """
        require_resp = _make_require_approval_response()
        denied_resp = _make_approval_status_response(
            "denied",
            reason="policy-required-approval",
            decision_notes="Too risky for this client.",
        )

        async def fake_post(path, data, action_type=None):
            if path == "/v1/chains":
                return _make_chain_start_response()
            if path.endswith("/complete"):
                return {"id": "chain-approval-001", "status": "completed"}
            return require_resp

        async def fake_get(path):
            return denied_resp

        with patch("arclasp.client._post", side_effect=fake_post), \
             patch("arclasp.client._get", side_effect=fake_get), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(ActionDeniedError) as exc_info:
                async with Chain("test-denied") as chain:
                    await chain.record_agent_action(
                        agent_name="finance-agent",
                        action_type="tool_call",
                        action_name="wire_transfer",
                        payload={"amount_usd": 6000},
                    )

        err = exc_info.value
        assert err.policy_name == "human_approval_denied"
        assert err.decision_source == "human_approval"
        assert err.condition == "Too risky for this client."
        assert err.remediation is not None
        assert "denied by a human approver" in err.remediation
        assert err.docs_url is not None
        assert "denials" in err.docs_url

    @pytest.mark.asyncio
    async def test_timed_out_raises_action_denied_error(self):
        """
        When the backend reports timed_out, ActionDeniedError must be raised
        with policy_name="approval_timeout" and decision_source="human_approval".
        """
        require_resp = _make_require_approval_response()
        timedout_resp = _make_approval_status_response("timed_out", reason=None)

        async def fake_post(path, data, action_type=None):
            if path == "/v1/chains":
                return _make_chain_start_response()
            if path.endswith("/complete"):
                return {"id": "chain-approval-001", "status": "completed"}
            return require_resp

        async def fake_get(path):
            return timedout_resp

        with patch("arclasp.client._post", side_effect=fake_post), \
             patch("arclasp.client._get", side_effect=fake_get), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(ActionDeniedError) as exc_info:
                async with Chain("test-timeout") as chain:
                    await chain.record_agent_action(
                        agent_name="finance-agent",
                        action_type="tool_call",
                        action_name="wire_transfer",
                        payload={"amount_usd": 6000},
                    )

        err = exc_info.value
        assert err.policy_name == "approval_timeout"
        assert err.decision_source == "human_approval"
        assert err.condition == "Approval was not resolved within the configured timeout window."
        assert err.remediation is not None
        assert "default_approval_timeout_hours" in err.remediation
        assert err.docs_url is not None
        assert "approvals" in err.docs_url
