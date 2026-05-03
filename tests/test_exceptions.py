"""
Tests for proofrail.exceptions — Item A verification.
"""

import pytest

from proofrail.exceptions import (
    ActionDeniedError,
    PolicyViolationError,
    ProofRailPolicyError,
)


class TestBaseClass:
    def test_action_denied_is_policy_error(self):
        err = ActionDeniedError(message="test")
        assert isinstance(err, ProofRailPolicyError)

    def test_policy_violation_is_policy_error(self):
        err = PolicyViolationError(message="test")
        assert isinstance(err, ProofRailPolicyError)

    def test_catch_both_with_base(self):
        caught = []
        for cls in (ActionDeniedError, PolicyViolationError):
            try:
                raise cls(message="boom")
            except ProofRailPolicyError as e:
                caught.append(type(e))
        assert caught == [ActionDeniedError, PolicyViolationError]

    def test_subclasses_still_distinct(self):
        with pytest.raises(ActionDeniedError):
            raise ActionDeniedError(message="x")
        with pytest.raises(PolicyViolationError):
            raise PolicyViolationError(message="x")


class TestFields:
    ALL_FIELDS = dict(
        message="msg",
        policy_name="cumulative_financial_threshold",
        condition="$12,400 exceeds $10,000",
        chain_context={"chain_id": "abc-123", "sequence": 3},
        remediation="Update threshold in init()",
        docs_url="https://docs.proofrail.ai/policies/thresholds",
        decision_source="backend_evaluation",
    )

    def _check_fields(self, err):
        assert err.message == "msg"
        assert err.policy_name == "cumulative_financial_threshold"
        assert err.condition == "$12,400 exceeds $10,000"
        assert err.chain_context == {"chain_id": "abc-123", "sequence": 3}
        assert err.remediation == "Update threshold in init()"
        assert err.docs_url == "https://docs.proofrail.ai/policies/thresholds"
        assert err.decision_source == "backend_evaluation"

    def test_action_denied_has_all_fields(self):
        self._check_fields(ActionDeniedError(**self.ALL_FIELDS))

    def test_policy_violation_has_all_fields(self):
        self._check_fields(PolicyViolationError(**self.ALL_FIELDS))

    def test_defaults_are_none(self):
        err = ActionDeniedError(message="bare")
        assert err.policy_name is None
        assert err.condition is None
        assert err.chain_context is None
        assert err.remediation is None
        assert err.docs_url is None
        assert err.decision_source is None


class TestStr:
    def test_str_contains_policy_name(self):
        err = ActionDeniedError(message="denied", policy_name="bulk_operation")
        s = str(err)
        assert "bulk_operation" in s

    def test_str_contains_condition(self):
        err = ActionDeniedError(message="denied", condition="batch size 500 > limit 100")
        assert "batch size 500 > limit 100" in str(err)

    def test_str_contains_remediation(self):
        err = ActionDeniedError(message="denied", remediation="Reduce batch size")
        assert "Reduce batch size" in str(err)

    def test_str_contains_docs_url(self):
        err = ActionDeniedError(message="denied", docs_url="https://docs.proofrail.ai/x")
        assert "https://docs.proofrail.ai/x" in str(err)

    def test_str_omits_none_fields(self):
        err = ActionDeniedError(message="bare")
        s = str(err)
        assert "Policy" not in s
        assert "Condition" not in s
        assert "Remediation" not in s

    def test_str_prefix_matches_class_name(self):
        assert str(ActionDeniedError(message="x")).startswith("ActionDeniedError:")
        assert str(PolicyViolationError(message="x")).startswith("PolicyViolationError:")
