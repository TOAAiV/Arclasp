"""
Tests for PolicyDecision Pydantic model — Item B verification.
"""

import pytest
from pydantic import ValidationError

from proofrail.models import PolicyDecision, RemediationV1


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


# ---------------------------------------------------------------------------
# REM1 — structured remediation_v1 field compatibility
#
# remediation_v1 is additive and independent of the pre-existing plain-string
# `remediation`/`docs_url` fields (see BACKEND_DENY above, which still uses
# the string form and must keep working unchanged).
# ---------------------------------------------------------------------------

BACKEND_DENY_WITH_REMEDIATION_V1 = {
    "policy_decision": "deny",
    "decision_reason": "Credential exposure detected in payload",
    "decision_source": "backend_evaluation",
    "policy_name": "credential_exposure",
    "remediation_v1": {
        "schema_version": 1,
        "outcome": "blocked",
        "retryable": True,
        "requires_human": False,
        "actions": [
            {
                "code": "reduce_scope",
                "summary": "Remove credentials, secrets, or tokens from the request payload before resubmitting.",
            }
        ],
        "details": {},
    },
}


class TestRemediationV1Compatibility:
    def test_new_server_field_parses_into_typed_model(self):
        d = PolicyDecision.model_validate(BACKEND_DENY_WITH_REMEDIATION_V1)
        assert isinstance(d.remediation_v1, RemediationV1)
        assert d.remediation_v1.schema_version == 1
        assert d.remediation_v1.outcome == "blocked"
        assert d.remediation_v1.retryable is True
        assert d.remediation_v1.requires_human is False

    def test_action_list_parses(self):
        d = PolicyDecision.model_validate(BACKEND_DENY_WITH_REMEDIATION_V1)
        assert len(d.remediation_v1.actions) == 1
        assert d.remediation_v1.actions[0].code == "reduce_scope"
        assert "credentials" in d.remediation_v1.actions[0].summary

    def test_old_server_response_missing_field_defaults_to_none(self):
        # BACKEND_DENY predates REM1 and has no remediation_v1 key at all.
        d = PolicyDecision.model_validate(BACKEND_DENY)
        assert d.remediation_v1 is None
        # The pre-existing plain-string fallback fields are unaffected.
        assert d.remediation == "Update financial_approval_threshold_usd in init()"
        assert d.docs_url == "https://docs.proofrail.ai/policies/thresholds"

    def test_legacy_remediation_string_still_parses(self):
        d = PolicyDecision.model_validate(BACKEND_DENY)
        assert d.remediation is not None
        assert d.docs_url is not None
        assert d.remediation_v1 is None

    def test_remediation_v1_present_alongside_legacy_string_fields(self):
        data = {**BACKEND_DENY, **BACKEND_DENY_WITH_REMEDIATION_V1}
        d = PolicyDecision.model_validate(data)
        assert d.remediation is not None  # legacy string field still present
        assert d.remediation_v1 is not None  # new structured field also present
        assert d.remediation_v1.outcome == "blocked"

    def test_round_trip_serialization_is_stable(self):
        d = PolicyDecision.model_validate(BACKEND_DENY_WITH_REMEDIATION_V1)
        dumped = d.model_dump(mode="json")
        redumped = PolicyDecision.model_validate(dumped)
        assert redumped.remediation_v1 == d.remediation_v1

    def test_unknown_future_outcome_or_action_code_does_not_raise(self):
        # A future backend schema-v1 extension adds an outcome/action code
        # this SDK version doesn't know about yet — both fields are plain
        # str, so this must still parse rather than reject the response.
        data = {
            **BACKEND_ALLOW,
            "remediation_v1": {
                "schema_version": 1,
                "outcome": "some_future_outcome",
                "retryable": False,
                "requires_human": False,
                "actions": [{"code": "some_future_action", "summary": "x"}],
                "details": {},
            },
        }
        d = PolicyDecision.model_validate(data)
        assert d.remediation_v1.outcome == "some_future_outcome"

    def test_malformed_remediation_v1_wrong_type_raises(self):
        # remediation_v1 must be an object, not a string — existing Pydantic
        # v2 behavior: a str given where a nested BaseModel is expected fails
        # validation rather than being silently coerced or dropped.
        data = {**BACKEND_ALLOW, "remediation_v1": "not-an-object"}
        with pytest.raises(ValidationError):
            PolicyDecision.model_validate(data)

    def test_malformed_remediation_v1_missing_required_subfield_raises(self):
        # schema_version/outcome/retryable/requires_human have no defaults —
        # omitting one is a validation error, not a silently-partial object.
        data = {
            **BACKEND_ALLOW,
            "remediation_v1": {
                "outcome": "blocked",
                "retryable": True,
                "requires_human": True,
                "actions": [],
                "details": {},
                # "schema_version" intentionally omitted
            },
        }
        with pytest.raises(ValidationError):
            PolicyDecision.model_validate(data)

    def test_unrelated_unknown_fields_ignored_alongside_remediation_v1(self):
        # Existing extra-fields-ignored behavior (test_extra_fields_ignored
        # above) must keep holding even when remediation_v1 is also present.
        data = {**BACKEND_DENY_WITH_REMEDIATION_V1, "some_future_unrelated_field": {"x": 1}}
        d = PolicyDecision.model_validate(data)
        assert d.remediation_v1.outcome == "blocked"
        assert not hasattr(d, "some_future_unrelated_field")
