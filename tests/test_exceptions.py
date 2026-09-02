"""
Tests for arclasp.exceptions first-release policy exception contract.
"""

import pytest

from arclasp.exceptions import (
    ActionDeniedError,
    ArclaspPolicyError,
    ChainAutoPausedError,
)


class TestBaseClass:
    def test_action_denied_is_policy_error(self):
        err = ActionDeniedError(message="test")
        assert isinstance(err, ArclaspPolicyError)

    def test_auto_pause_is_policy_error(self):
        err = ChainAutoPausedError()
        assert isinstance(err, ArclaspPolicyError)

    def test_catch_policy_family_with_base(self):
        caught = []
        for exc in (
            ActionDeniedError(message="boom"),
            ChainAutoPausedError(chain_id="abc-123"),
        ):
            try:
                raise exc
            except ArclaspPolicyError as e:
                caught.append(type(e))
        assert caught == [ActionDeniedError, ChainAutoPausedError]

    def test_subclasses_still_distinct(self):
        with pytest.raises(ActionDeniedError):
            raise ActionDeniedError(message="x")
        with pytest.raises(ChainAutoPausedError):
            raise ChainAutoPausedError(message="x")


class TestFields:
    ALL_FIELDS = dict(
        message="msg",
        policy_name="cumulative_financial_threshold",
        condition="$12,400 exceeds $10,000",
        chain_context={"chain_id": "abc-123", "sequence": 3},
        remediation="Update threshold in init()",
        docs_url="https://docs.proofrail.dev/policies/thresholds",
        decision_source="backend_evaluation",
    )

    def _check_fields(self, err):
        assert err.message == "msg"
        assert err.policy_name == "cumulative_financial_threshold"
        assert err.condition == "$12,400 exceeds $10,000"
        assert err.chain_context == {"chain_id": "abc-123", "sequence": 3}
        assert err.remediation == "Update threshold in init()"
        assert err.docs_url == "https://docs.proofrail.dev/policies/thresholds"
        assert err.decision_source == "backend_evaluation"

    def test_action_denied_has_all_fields(self):
        self._check_fields(ActionDeniedError(**self.ALL_FIELDS))

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
        err = ActionDeniedError(message="denied", docs_url="https://docs.proofrail.dev/x")
        assert "https://docs.proofrail.dev/x" in str(err)

    def test_str_omits_none_fields(self):
        err = ActionDeniedError(message="bare")
        s = str(err)
        assert "Policy" not in s
        assert "Condition" not in s
        assert "Remediation" not in s

    def test_str_prefix_matches_class_name(self):
        assert str(ActionDeniedError(message="x")).startswith("ActionDeniedError:")


class TestChainAutoPausedError:
    """ChainAutoPausedError contract."""

    def test_is_policy_error(self):
        err = ChainAutoPausedError()
        assert isinstance(err, ArclaspPolicyError)

    def test_catchable_via_base(self):
        caught = []
        try:
            raise ChainAutoPausedError(chain_id="abc-123")
        except ArclaspPolicyError as e:
            caught.append(type(e))
        assert caught == [ChainAutoPausedError]

    def test_default_message(self):
        err = ChainAutoPausedError()
        assert "auto-paused" in err.message.lower()
        assert err.chain_id is None
        assert err.reason is None

    def test_chain_id_stored(self):
        err = ChainAutoPausedError(chain_id="chain-999")
        assert err.chain_id == "chain-999"
        assert err.chain_context == {"chain_id": "chain-999"}

    def test_reason_stored_as_condition(self):
        err = ChainAutoPausedError(chain_id="c1", reason="too many actions")
        assert err.reason == "too many actions"
        assert err.condition == "too many actions"

    def test_policy_name_is_auto_pause(self):
        err = ChainAutoPausedError()
        assert err.policy_name == "auto_pause"

    def test_docs_url_matches_sdk_convention(self):
        err = ChainAutoPausedError()
        assert err.docs_url == "https://docs.arclasp.com/policies/runaway-limits"

    def test_remediation_includes_resume_hint(self):
        err = ChainAutoPausedError(chain_id="chain-xyz")
        assert "resume" in err.remediation.lower()
        assert "chain-xyz" in err.remediation

    def test_remediation_without_chain_id(self):
        err = ChainAutoPausedError()
        assert err.remediation is not None
        assert "resume" in err.remediation.lower()

    def test_custom_message(self):
        err = ChainAutoPausedError(message="custom pause message", chain_id="c2")
        assert err.message == "custom pause message"

    def test_str_contains_policy_name(self):
        err = ChainAutoPausedError(chain_id="c3")
        assert "auto_pause" in str(err)

    def test_str_contains_chain_context(self):
        err = ChainAutoPausedError(chain_id="c3")
        assert "c3" in str(err)

    def test_decision_source_is_backend(self):
        err = ChainAutoPausedError()
        assert err.decision_source == "backend_evaluation"
