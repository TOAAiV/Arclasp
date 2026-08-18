"""
Tests for arclasp.policies — classify_risk, update_chain_metrics_local,
evaluate_policy, and process_action_local.
"""

from __future__ import annotations

import pytest

from arclasp.policies import (
    classify_risk,
    evaluate_policy,
    process_action_local,
    update_chain_metrics_local,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _risk(action_type="tool_call", action_name="get_record", payload=None,
          agent_name="agent", chain_context=None):
    return classify_risk(
        action_type, action_name, payload or {}, agent_name, chain_context or {}
    )


def _policy(action_type="tool_call", action_name="get_record", payload=None,
            agent_name="agent", risk=None, metrics=None, org_config=None):
    if risk is None:
        risk = _risk(action_type, action_name, payload or {}, agent_name)
    return evaluate_policy(
        action_type, action_name, payload or {}, agent_name,
        risk, metrics or {}, org_config or {},
    )


# ===========================================================================
# Stage 1 — classify_risk
# ===========================================================================

class TestClassifyRisk:
    def test_empty_payload_zero_score(self):
        r = _risk(payload={})
        assert r["risk_score"] == 0
        assert r["categories"] == []
        assert r["reasons"] == []

    def test_delete_action_type_scores_destructive(self):
        r = _risk(action_type="delete", action_name="remove_item")
        assert "destructive" in r["categories"]
        assert r["risk_score"] >= 40

    def test_delete_in_action_name_scores_destructive(self):
        r = _risk(action_type="tool_call", action_name="delete_record")
        assert "destructive" in r["categories"]
        assert r["risk_score"] >= 40

    def test_send_email_scores_communication(self):
        r = _risk(action_name="send_email")
        assert "communication" in r["categories"]
        assert r["risk_score"] >= 25

    def test_high_financial_value_financial_high(self):
        r = _risk(payload={"amount": 15000})
        assert "financial_high" in r["categories"]
        assert r["risk_score"] >= 50

    def test_medium_financial_value_financial(self):
        r = _risk(payload={"amount": 2500})
        assert "financial" in r["categories"]
        assert "financial_high" not in r["categories"]
        assert r["risk_score"] >= 30

    def test_low_financial_value_no_category(self):
        # Below 1000 — no financial category
        r = _risk(payload={"amount": 500})
        assert "financial" not in r["categories"]
        assert "financial_high" not in r["categories"]

    def test_sensitive_payload_credential_exposure(self):
        r = _risk(payload={"api_key": "sk-secret123"})
        assert "credential_exposure" in r["categories"]
        assert r["risk_score"] >= 50

    def test_all_categories_score_caps_at_100(self):
        # delete (+40) + financial_high (+50) + credential (+50) = 140 → capped at 100
        r = classify_risk(
            "delete", "delete_record",
            {"amount": 50000, "api_key": "sk-secret"},
            "normal-agent", {},
        )
        assert r["risk_score"] == 100

    def test_write_in_action_name_scores_write(self):
        r = _risk(action_name="write_file")
        assert "write" in r["categories"]
        assert r["risk_score"] >= 20

    def test_iam_in_action_name_scores_privilege_escalation(self):
        r = _risk(action_name="update_iam_policy")
        assert "privilege_escalation" in r["categories"]
        assert r["risk_score"] >= 40

    def test_url_in_payload_scores_exfiltration(self):
        r = _risk(payload={"url": "https://example.com"})
        assert "exfiltration" in r["categories"]


# ===========================================================================
# Stage 2 — update_chain_metrics_local
# ===========================================================================

class TestUpdateChainMetricsLocal:
    def test_financial_amount_accumulated(self):
        risk = {"categories": ["financial"], "risk_score": 30}
        result = update_chain_metrics_local({}, "tool_call", "pay", {"amount": 500}, risk)
        assert result["financial_exposure_usd"] == pytest.approx(500.0)

    def test_financial_amount_accumulates_on_existing(self):
        risk = {"categories": ["financial"], "risk_score": 30}
        existing = {"financial_exposure_usd": 1000.0}
        result = update_chain_metrics_local(
            existing, "tool_call", "pay", {"amount": 500}, risk
        )
        assert result["financial_exposure_usd"] == pytest.approx(1500.0)

    def test_communication_increments_count(self):
        risk = {"categories": ["communication"], "risk_score": 25}
        result = update_chain_metrics_local({}, "tool_call", "send_email", {}, risk)
        assert result["external_communications_count"] == 1

    def test_domain_appended_and_deduplicated(self):
        risk = {"categories": [], "risk_score": 0}
        r1 = update_chain_metrics_local(
            {}, "tool_call", "fetch", {"domain": "example.com"}, risk
        )
        r2 = update_chain_metrics_local(
            r1, "tool_call", "fetch", {"domain": "example.com"}, risk
        )
        assert r2["external_domains_contacted"] == ["example.com"]

    def test_does_not_mutate_input(self):
        risk = {"categories": ["financial"], "risk_score": 30}
        original = {"financial_exposure_usd": 100.0}
        update_chain_metrics_local(original, "tool_call", "pay", {"amount": 500}, risk)
        assert original["financial_exposure_usd"] == pytest.approx(100.0)

    def test_write_increments_records_modified(self):
        risk = {"categories": ["write"], "risk_score": 20}
        result = update_chain_metrics_local({}, "write", "update_row", {}, risk)
        assert result["records_modified_count"] == 1

    def test_privilege_escalation_increments_count(self):
        risk = {"categories": ["privilege_escalation"], "risk_score": 40}
        result = update_chain_metrics_local(
            {}, "tool_call", "update_iam_policy", {}, risk
        )
        assert result["privileged_actions_count"] == 1


# ===========================================================================
# Stage 3 — evaluate_policy
# ===========================================================================

class TestEvaluatePolicy:
    # --- Hard denies ---

    def test_hard_deny_production_delete(self):
        risk = {"categories": [], "risk_score": 0}
        result = evaluate_policy(
            "tool_call", "delete_record", {}, "agent", risk, {},
            {"environment": "production"},
        )
        assert result["decision"] == "deny"
        assert "production" in result["reason"].lower()

    def test_hard_deny_cumulative_over_50k(self):
        risk = {"categories": [], "risk_score": 0}
        metrics = {"financial_exposure_usd": 51_000.0}
        result = evaluate_policy(
            "tool_call", "pay", {}, "agent", risk, metrics, {}
        )
        assert result["decision"] == "deny"
        assert "50,000" in result["reason"]

    def test_hard_deny_credential_exposure(self):
        risk = {"categories": ["credential_exposure"], "risk_score": 50}
        result = evaluate_policy(
            "tool_call", "store_key", {}, "agent", risk, {}, {}
        )
        assert result["decision"] == "deny"

    def test_hard_deny_iam_in_action_name(self):
        risk = {"categories": [], "risk_score": 0}
        result = evaluate_policy(
            "tool_call", "update_iam_policy", {}, "agent", risk, {}, {}
        )
        assert result["decision"] == "deny"

    def test_hard_deny_permission_in_action_name(self):
        risk = {"categories": [], "risk_score": 0}
        result = evaluate_policy(
            "tool_call", "set_permission", {}, "agent", risk, {}, {}
        )
        assert result["decision"] == "deny"

    # --- Approval triggers ---

    def test_approval_single_transaction_over_threshold(self):
        risk = {"categories": ["financial_high"], "risk_score": 50}
        result = evaluate_policy(
            "tool_call", "transfer", {"amount": 7500}, "agent", risk, {},
            {"financial_approval_threshold_usd": 5000},
        )
        assert result["decision"] == "require_approval"

    def test_approval_cumulative_over_threshold(self):
        risk = {"categories": [], "risk_score": 0}
        metrics = {"financial_exposure_usd": 11_000.0}
        result = evaluate_policy(
            "tool_call", "pay", {}, "agent", risk, metrics,
            {"cumulative_threshold_usd": 10_000},
        )
        assert result["decision"] == "require_approval"

    def test_approval_first_external_communication(self):
        risk = {"categories": ["communication"], "risk_score": 25}
        metrics = {"external_communications_count": 1}
        result = evaluate_policy(
            "tool_call", "send_email", {}, "agent", risk, metrics, {}
        )
        assert result["decision"] == "require_approval"

    def test_no_approval_second_communication(self):
        # count == 2 → not the first → should not trigger this gate
        risk = {"categories": ["communication"], "risk_score": 25}
        metrics = {"external_communications_count": 2}
        result = evaluate_policy(
            "tool_call", "send_email", {}, "agent", risk, metrics, {}
        )
        # Should fall through to allow_with_flag (write not present; risk=25 < 40)
        assert result["decision"] not in ("require_approval", "deny")

    def test_approval_high_risk_score(self):
        risk = {"categories": [], "risk_score": 75}
        result = evaluate_policy(
            "tool_call", "action", {}, "agent", risk, {}, {}
        )
        assert result["decision"] == "require_approval"

    # --- Flag decisions ---

    def test_flag_write_category(self):
        risk = {"categories": ["write"], "risk_score": 20}
        result = evaluate_policy(
            "tool_call", "write_file", {}, "agent", risk, {}, {}
        )
        assert result["decision"] == "allow_with_flag"

    def test_flag_medium_risk_score(self):
        risk = {"categories": [], "risk_score": 45}
        result = evaluate_policy(
            "tool_call", "action", {}, "agent", risk, {}, {}
        )
        assert result["decision"] == "allow_with_flag"

    # --- Default allow ---

    def test_default_allow_low_risk(self):
        risk = {"categories": [], "risk_score": 0}
        result = evaluate_policy(
            "tool_call", "get_record", {}, "agent", risk, {}, {}
        )
        assert result["decision"] == "allow"

    def test_source_is_backend_evaluation(self):
        risk = {"categories": [], "risk_score": 0}
        result = evaluate_policy(
            "tool_call", "get_record", {}, "agent", risk, {}, {}
        )
        assert result["source"] == "backend_evaluation"


# ===========================================================================
# Orchestrator — process_action_local
# ===========================================================================

class TestProcessActionLocal:
    def test_kill_switch_returns_deny(self):
        result = process_action_local(
            agent_name="agent",
            action_type="tool_call",
            action_name="get_data",
            payload={},
            cumulative_metrics={},
            org_config={},
            kill_switch_active=True,
            kill_switch_reason="Security incident",
        )
        assert result["decision"] == "deny"
        assert result["kill_switch_active"] is True
        assert result["evaluation_mode"] == "enforce"

    def test_disabled_mode_returns_allow_no_risk_classification(self):
        result = process_action_local(
            agent_name="agent",
            action_type="tool_call",
            action_name="get_data",
            payload={},
            cumulative_metrics={},
            org_config={},
            policy_mode="disabled",
        )
        assert result["decision"] == "allow"
        assert result["evaluation_mode"] == "disabled"
        assert result["risk_classification"] == {}

    def test_shadow_mode_deny_action_returns_allow_with_shadow_decision(self):
        result = process_action_local(
            agent_name="agent",
            action_type="tool_call",
            action_name="delete_record",
            payload={},
            cumulative_metrics={},
            org_config={"environment": "production"},
            policy_mode="shadow",
        )
        assert result["decision"] == "allow"
        assert result["evaluation_mode"] == "shadow"
        assert result["shadow_decision"] == "deny"

    def test_enforce_budget_exceeded_upgrades_allow_to_require_approval(self):
        result = process_action_local(
            agent_name="agent",
            action_type="tool_call",
            action_name="generate_text",
            payload={},
            cumulative_metrics={},
            org_config={},
            policy_mode="enforce",
            monthly_budget_exceeded=True,
        )
        assert result["decision"] == "require_approval"
        assert "budget" in result["reason"].lower()

    def test_enforce_active_exception_downgrades_require_approval_to_allow(self):
        # send_email on first comm triggers require_approval, but exception bypasses it
        result = process_action_local(
            agent_name="agent",
            action_type="tool_call",
            action_name="send_email",
            payload={},
            cumulative_metrics={"external_communications_count": 0},
            org_config={},
            policy_mode="enforce",
            active_exception_id="exc-abc-123",
        )
        assert result["decision"] == "allow"
        assert "exc-abc-123" in result["reason"]

    def test_updated_cumulative_metrics_in_result(self):
        result = process_action_local(
            agent_name="agent",
            action_type="tool_call",
            action_name="pay",
            payload={"amount": 500},
            cumulative_metrics={},
            org_config={},
        )
        assert "updated_cumulative_metrics" in result
        assert result["updated_cumulative_metrics"]["financial_exposure_usd"] == pytest.approx(500.0)

    def test_source_is_sdk_reference_implementation(self):
        result = process_action_local(
            agent_name="agent",
            action_type="tool_call",
            action_name="get_data",
            payload={},
            cumulative_metrics={},
            org_config={},
        )
        assert result["source"] == "sdk_reference_implementation"

    def test_input_cumulative_metrics_not_mutated(self):
        original = {"financial_exposure_usd": 100.0}
        process_action_local(
            agent_name="agent",
            action_type="tool_call",
            action_name="pay",
            payload={"amount": 9999},
            cumulative_metrics=original,
            org_config={},
        )
        assert original["financial_exposure_usd"] == pytest.approx(100.0)

    def test_kill_switch_skips_risk_classification(self):
        result = process_action_local(
            agent_name="agent",
            action_type="tool_call",
            action_name="get_data",
            payload={},
            cumulative_metrics={},
            org_config={},
            kill_switch_active=True,
        )
        # risk_classification is {} when kill switch fires
        assert result["risk_classification"] == {}
