"""
Tests for the denial code path — Items C, E verification.

Regression test for bug #3: ActionDeniedError(decision_source=...) must not
raise TypeError.  The error must carry fully-populated diagnostic fields.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import arclasp
from arclasp.exceptions import ActionDeniedError, ArclaspPolicyError
from arclasp.chain import Chain


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_deny_response(
    policy_name: str,
    reason: str,
    remediation: str | None = None,
    docs_url: str | None = None,
) -> dict:
    return {
        "policy_decision": "deny",
        "decision_reason": reason,
        "decision_source": "backend_evaluation",
        "policy_name": policy_name,
        "remediation": remediation,
        "docs_url": docs_url,
    }


def _make_chain_start_response(chain_id: str = "test-chain-001") -> dict:
    return {"id": chain_id}


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def sdk_init():
    arclasp.init(
        api_key="prail_test",
        backend_url="http://localhost:9999",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDenialRaisesActionDeniedError:
    @pytest.mark.asyncio
    async def test_deny_raises_action_denied_not_type_error(self):
        """Regression: decision_source kwarg must not cause TypeError (bug #3)."""
        deny_resp = _make_deny_response(
            policy_name="cumulative_financial_threshold",
            reason="$12,400 exceeds $10,000",
        )

        async def fake_post(path, data, action_type=None, headers=None, config=None):
            if path == "/v1/chains":
                return _make_chain_start_response()
            return deny_resp

        with patch("arclasp.client._post", side_effect=fake_post):
            with pytest.raises(ActionDeniedError) as exc_info:
                async with Chain("test") as chain:
                    await chain.record_agent_action(
                        agent_name="agent",
                        action_type="tool_call",
                        action_name="transfer_funds",
                    )

        err = exc_info.value
        assert isinstance(err, ActionDeniedError)
        assert isinstance(err, ArclaspPolicyError)

    @pytest.mark.asyncio
    async def test_action_denied_not_type_error(self):
        """decision_source is a valid kwarg on ActionDeniedError after Item A."""
        err = ActionDeniedError(
            message="denied",
            decision_source="backend_evaluation",
        )
        assert err.decision_source == "backend_evaluation"


class TestDenialErrorFields:
    @pytest.mark.asyncio
    async def test_cumulative_financial_threshold_fields(self):
        deny_resp = _make_deny_response(
            policy_name="cumulative_financial_threshold",
            reason="Cumulative exposure $12,400 exceeds threshold $10,000",
        )

        async def fake_post(path, data, action_type=None, headers=None, config=None):
            if path == "/v1/chains":
                return _make_chain_start_response()
            return deny_resp

        with patch("arclasp.client._post", side_effect=fake_post):
            with pytest.raises(ActionDeniedError) as exc_info:
                async with Chain("test") as chain:
                    await chain.record_agent_action(
                        agent_name="finance-agent",
                        action_type="tool_call",
                        action_name="wire_transfer",
                    )

        err = exc_info.value
        assert err.policy_name == "cumulative_financial_threshold"
        assert err.condition == "Cumulative exposure $12,400 exceeds threshold $10,000"
        assert err.decision_source == "backend_evaluation"
        # SDK should fill in remediation from built-in lookup when backend omits it
        assert err.remediation is not None
        assert "policy_config" in err.remediation or "organization policy" in err.remediation
        assert err.docs_url is not None
        assert "thresholds" in err.docs_url

    @pytest.mark.asyncio
    async def test_unauthorized_domain_fields(self):
        deny_resp = _make_deny_response(
            policy_name="unauthorized_domain",
            reason="Domain evil.com is not in the allowlist",
        )

        async def fake_post(path, data, action_type=None, headers=None, config=None):
            if path == "/v1/chains":
                return _make_chain_start_response()
            return deny_resp

        with patch("arclasp.client._post", side_effect=fake_post):
            with pytest.raises(ActionDeniedError) as exc_info:
                async with Chain("test") as chain:
                    await chain.record_agent_action(
                        agent_name="web-agent",
                        action_type="tool_call",
                        action_name="fetch_url",
                        payload={"url": "http://evil.com"},
                    )

        err = exc_info.value
        assert err.policy_name == "unauthorized_domain"
        assert err.remediation is not None
        assert "organization policy allowlist" in err.remediation
        assert err.docs_url is not None
        assert "domains" in err.docs_url

    @pytest.mark.asyncio
    async def test_bulk_operation_fields(self):
        deny_resp = _make_deny_response(
            policy_name="bulk_operation",
            reason="Batch size 500 exceeds limit 100",
        )

        async def fake_post(path, data, action_type=None, headers=None, config=None):
            if path == "/v1/chains":
                return _make_chain_start_response()
            return deny_resp

        with patch("arclasp.client._post", side_effect=fake_post):
            with pytest.raises(ActionDeniedError) as exc_info:
                async with Chain("test") as chain:
                    await chain.record_agent_action(
                        agent_name="batch-agent",
                        action_type="tool_call",
                        action_name="bulk_delete",
                        payload={"count": 500},
                    )

        err = exc_info.value
        assert err.policy_name == "bulk_operation"
        assert err.remediation is not None
        assert err.docs_url is not None

    @pytest.mark.asyncio
    async def test_backend_supplied_remediation_takes_precedence(self):
        """When the backend returns remediation/docs_url, use them, not the SDK defaults."""
        deny_resp = _make_deny_response(
            policy_name="cumulative_financial_threshold",
            reason="Over limit",
            remediation="Contact your account manager.",
            docs_url="https://custom.docs/specific-page",
        )

        async def fake_post(path, data, action_type=None, headers=None, config=None):
            if path == "/v1/chains":
                return _make_chain_start_response()
            return deny_resp

        with patch("arclasp.client._post", side_effect=fake_post):
            with pytest.raises(ActionDeniedError) as exc_info:
                async with Chain("test") as chain:
                    await chain.record_agent_action(
                        agent_name="agent",
                        action_type="tool_call",
                        action_name="action",
                    )

        err = exc_info.value
        assert err.remediation == "Contact your account manager."
        assert err.docs_url == "https://custom.docs/specific-page"

    @pytest.mark.asyncio
    async def test_chain_context_populated(self):
        deny_resp = _make_deny_response(
            policy_name="bulk_operation",
            reason="Too big",
        )

        async def fake_post(path, data, action_type=None, headers=None, config=None):
            if path == "/v1/chains":
                return _make_chain_start_response("chain-xyz")
            return deny_resp

        with patch("arclasp.client._post", side_effect=fake_post):
            with pytest.raises(ActionDeniedError) as exc_info:
                async with Chain("test") as chain:
                    await chain.record_agent_action(
                        agent_name="agent",
                        action_type="tool_call",
                        action_name="bulk_op",
                    )

        err = exc_info.value
        assert err.chain_context is not None
        assert err.chain_context.get("chain_id") == "chain-xyz"
