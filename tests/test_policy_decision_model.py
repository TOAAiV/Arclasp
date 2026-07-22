"""
Tests for PolicyDecision Pydantic model — Item B verification.
"""

import pytest
from pydantic import ValidationError

from proofrail.models import PolicyDecision


# Representative backend response shapes

BACKEND_ALLOW = {
    "policy_decision": "allow",
    "decision_reason": "All checks passed",
    "decision_source": "backend_evaluation",
}

BACKEND_ALLOW_WITH_FLAG = {
    "policy_decision": "allow_with_flag",
    "decision_reason": "Action allowed but flagged for review",
    "decision_source": "backend_evaluation",
    "policy_name": "high_risk_agent",
}

BACKEND_REQUIRE_APPROVAL = {
    "policy_decision": "require_approval",
    "decision_reason": "Pending human approval",
    "decision_source": "backend_evaluation",
    "policy_name": "cumulative_financial_threshold",
}

BACKEND_DENY = {
    "policy_decision": "deny",
    "decision_reason": "Chain cumulative exposure of $12,400 exceeds configured threshold of $10,000",
    "decision_source": "backend_evaluation",
    "policy_name": "cumulative_financial_threshold",
    "remediation": "Update financial_approval_threshold_usd in init()",
    "docs_url": "https://docs.proofrail.ai/policies/thresholds",
}

BACKEND_DENY_KILL_SWITCH = {
    "policy_decision": "deny",
    "decision_reason": "Kill switch active",
    "decision_source": "backend_evaluation",
    "kill_switch_active": True,
    "pause_reason": "Security incident in progress",
}

OFFLINE_STUB = {
    "policy_decision": "allow",
    "decision_reason": "Offline — fail_mode=allow",
    "decision_source": "offline_stub",
}


class TestModelValidate:
    def test_allow_parses(self):
        d = PolicyDecision.model_validate(BACKEND_ALLOW)
        assert d.policy_decision == "allow"
        assert d.decision_reason == "All checks passed"
        assert d.decision_source == "backend_evaluation"

    def test_allow_with_flag_parses(self):
        d = PolicyDecision.model_validate(BACKEND_ALLOW_WITH_FLAG)
        assert d.policy_decision == "allow_with_flag"
        assert d.policy_name == "high_risk_agent"

    def test_require_approval_parses(self):
        d = PolicyDecision.model_validate(BACKEND_REQUIRE_APPROVAL)
        assert d.policy_decision == "require_approval"
        assert d.policy_name == "cumulative_financial_threshold"

    def test_deny_parses_with_optional_fields(self):
        d = PolicyDecision.model_validate(BACKEND_DENY)
        assert d.policy_decision == "deny"
        assert d.policy_name == "cumulative_financial_threshold"
        assert d.remediation is not None
        assert d.docs_url is not None

    def test_kill_switch_deny_parses(self):
        d = PolicyDecision.model_validate(BACKEND_DENY_KILL_SWITCH)
        assert d.policy_decision == "deny"
        assert d.kill_switch_active is True
        assert d.pause_reason == "Security incident in progress"

    def test_offline_stub_parses(self):
        d = PolicyDecision.model_validate(OFFLINE_STUB)
        assert d.policy_decision == "allow"
        assert d.decision_source == "offline_stub"

    def test_optional_fields_default_to_none(self):
        d = PolicyDecision.model_validate(BACKEND_ALLOW)
        assert d.policy_name is None
        assert d.kill_switch_active is False
        assert d.pause_reason is None
        assert d.remediation is None
        assert d.docs_url is None

    def test_missing_required_field_raises(self):
        with pytest.raises(ValidationError):
            PolicyDecision.model_validate({"decision_reason": "x"})

    def test_extra_fields_ignored(self):
        # Backend may add new fields — model should not choke on unknowns
        data = {**BACKEND_ALLOW, "future_field": "some_value"}
        d = PolicyDecision.model_validate(data)
        assert d.policy_decision == "allow"

    def test_direct_construction(self):
        d = PolicyDecision(
            policy_decision="allow",
            decision_reason="",
            decision_source="offline_stub",
        )
        assert d.policy_decision == "allow"

    def test_policy_decision_preserves_cumulative_metrics_on_model_validate(self):
        d = PolicyDecision.model_validate({
            "policy_decision": "allow",
            "decision_reason": "test",
            "auto_paused": False,
            "cumulative_metrics": {
                "financial_exposure_usd": 5000,
                "records_modified_count": 3,
            },
        })
        assert d.cumulative_metrics == {
            "financial_exposure_usd": 5000,
            "records_modified_count": 3,
        }
