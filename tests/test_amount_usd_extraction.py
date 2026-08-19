"""
Regression tests: arclasp.policies must read the "amount_usd" payload key.

Bug: the SDK's own documented payload shape is {"amount_usd": <n>} (see
Chain.record_agent_action docstring, all demos/*.py), but _extract_numeric()
only checked ["amount", "value"], so update_chain_metrics_local() never
accumulated financial_exposure_usd for real user payloads and
evaluate_policy() never crossed the cumulative threshold.

These are pure unit tests — no HTTP, no backend — exercising the local SDK
policy module directly, matching the style of test_policies.py.
"""

from __future__ import annotations


from arclasp.policies import (
    _extract_numeric,
    evaluate_policy,
    update_chain_metrics_local,
)


class TestExtractNumericAmountUsd:

    def test_amount_usd_is_read(self):
        assert _extract_numeric({"amount_usd": 4000}, ["amount_usd", "amount", "value"]) == 4000.0

    def test_amount_usd_preferred_over_amount(self):
        payload = {"amount_usd": 4000, "amount": 999}
        assert _extract_numeric(payload, ["amount_usd", "amount", "value"]) == 4000.0

    def test_legacy_amount_key_still_works(self):
        """Backward compat: existing callers using {"amount": ...} keep working."""
        assert _extract_numeric({"amount": 500}, ["amount_usd", "amount", "value"]) == 500.0


class TestUpdateChainMetricsLocalAmountUsd:

    def test_single_amount_usd_event_increments_financial_exposure(self):
        risk = {"categories": []}
        metrics = update_chain_metrics_local({}, "tool_call", "record_commitment",
                                              {"amount_usd": 4000}, risk)
        assert metrics["financial_exposure_usd"] == 4000.0

    def test_three_amount_usd_events_accumulate_to_11000(self):
        """
        Reproduces the exact smoke-test scenario: $4000 + $3000 + $4000 across
        three record_commitment events must sum to $11,000.
        """
        risk = {"categories": []}
        metrics: dict = {}
        for amount in (4000, 3000, 4000):
            metrics = update_chain_metrics_local(
                metrics, "tool_call", "record_commitment", {"amount_usd": amount}, risk
            )
        assert metrics["financial_exposure_usd"] == 11_000.0


class TestEvaluatePolicyCumulativeThresholdAmountUsd:

    def test_11000_cumulative_triggers_require_approval(self):
        """
        With cumulative_financial_exposure=$11,000 built entirely from
        amount_usd payloads and org cumulative_threshold_usd=10000 (matching
        arclasp.init(cumulative_financial_threshold_usd=10000)), the
        decision must be require_approval.
        """
        org_config = {
            "environment": "production",
            "financial_approval_threshold_usd": 5_000,
            "cumulative_threshold_usd": 10_000,
        }
        risk = {"categories": [], "risk_score": 0}
        metrics: dict = {}
        for amount in (4000, 3000, 4000):
            metrics = update_chain_metrics_local(
                metrics, "tool_call", "record_commitment", {"amount_usd": amount}, risk
            )

        result = evaluate_policy(
            action_type="tool_call",
            action_name="record_commitment",
            payload={"amount_usd": 4000},
            agent_name="commitment",
            risk_classification=risk,
            cumulative_metrics=metrics,
            org_config=org_config,
        )

        assert result["decision"] == "require_approval"
