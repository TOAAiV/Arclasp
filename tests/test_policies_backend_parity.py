"""
Parity tests: compare proofrail.policies against backend.app.services.policy_engine.

For every (inputs, expected_decision) tuple, BOTH the SDK's classify_risk /
evaluate_policy and the backend's are called.  If results differ, the test
fails and we surface the divergence — we never "fix" either side silently.

Import strategy
---------------
The backend's policy_engine.py has module-level imports of SQLAlchemy and
app.* ORM models.  Those are DB-bound and unavailable in the SDK test context.
We mock them via sys.modules BEFORE the backend module is imported; the pure
functions (classify_risk, evaluate_policy) work fine because they have no
SQLAlchemy calls in their bodies.

If the backend module cannot be loaded for any reason (e.g. the test runner
runs from a path where backend/ is not a Python package), all tests in this
file are skipped with an informative message.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import MagicMock

# ------------------------------------------------------------------
# Mock DB-dependent modules BEFORE loading the backend.
# sys.modules.setdefault() is a no-op if the key already exists, so
# if SQLAlchemy IS installed we don't shadow it.
# ------------------------------------------------------------------
for _mock_name in [
    "app",
    "app.models",
    "app.models.chain",
    "app.models.daily_cost_summary",
    "app.models.organization",
    "app.models.policy_exception",
    "app.services",
    "app.services.cost_calculator",
]:
    sys.modules.setdefault(_mock_name, MagicMock())

import pytest  # noqa: E402

# ------------------------------------------------------------------
# Attempt to load the backend.  All tests in this file are skipped if
# the import fails.
# ------------------------------------------------------------------
BACKEND_AVAILABLE = False
_backend_classify_risk = None
_backend_evaluate_policy = None

try:
    from backend.app.services import policy_engine as _bpe  # type: ignore[import]
    _backend_classify_risk = _bpe.classify_risk
    _backend_evaluate_policy = _bpe.evaluate_policy
    BACKEND_AVAILABLE = True
except Exception as _import_exc:
    _BACKEND_IMPORT_ERROR = str(_import_exc)

from proofrail.policies import classify_risk as _sdk_classify_risk  # noqa: E402
from proofrail.policies import evaluate_policy as _sdk_evaluate_policy  # noqa: E402

_skip_no_backend = pytest.mark.skipif(
    not BACKEND_AVAILABLE,
    reason="backend package not importable — run from worktree root with backend in PYTHONPATH",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _call_backend_classify(action_type, action_name, payload, agent_name, chain_context):
    """Run the async backend classify_risk synchronously."""
    return asyncio.run(
        _backend_classify_risk(action_type, action_name, payload, agent_name, chain_context)
    )


def _call_backend_evaluate(action_type, action_name, payload, agent_name,
                            risk, metrics, org_config):
    return asyncio.run(
        _backend_evaluate_policy(action_type, action_name, payload, agent_name,
                                  risk, metrics, org_config)
    )


def _assert_classify_parity(action_type, action_name, payload, agent_name,
                              chain_context=None):
    """Assert SDK classify_risk == backend classify_risk for given inputs."""
    ctx = dict(chain_context or {})
    # Provide registered_agents so both sides agree on registration status.
    # Tests that want "all unregistered" behaviour pass registered_agents={} explicitly.
    if "registered_agents" not in ctx:
        ctx["registered_agents"] = {agent_name.lower(): {"risk_tier": "standard"}}
    sdk_result = _sdk_classify_risk(action_type, action_name, payload, agent_name, ctx)
    backend_result = _call_backend_classify(action_type, action_name, payload, agent_name, ctx)

    assert sdk_result["risk_score"] == backend_result["risk_score"], (
        f"risk_score mismatch for ({action_type!r}, {action_name!r}):\n"
        f"  SDK:     {sdk_result}\n"
        f"  Backend: {backend_result}"
    )
    assert sorted(sdk_result["categories"]) == sorted(backend_result["categories"]), (
        f"categories mismatch for ({action_type!r}, {action_name!r}):\n"
        f"  SDK:     {sdk_result['categories']}\n"
        f"  Backend: {backend_result['categories']}"
    )


def _assert_evaluate_parity(action_type, action_name, payload, agent_name,
                              risk, metrics, org_config):
    """Assert SDK evaluate_policy == backend evaluate_policy (decision only)."""
    sdk_result = _sdk_evaluate_policy(
        action_type, action_name, payload, agent_name, risk, metrics, org_config
    )
    backend_result = _call_backend_evaluate(
        action_type, action_name, payload, agent_name, risk, metrics, org_config
    )
    assert sdk_result["decision"] == backend_result["decision"], (
        f"decision mismatch for ({action_type!r}, {action_name!r}):\n"
        f"  SDK:     {sdk_result}\n"
        f"  Backend: {backend_result}"
    )
    assert sdk_result["reason"] == backend_result["reason"], (
        f"reason mismatch for ({action_type!r}, {action_name!r}):\n"
        f"  SDK:     {sdk_result['reason']!r}\n"
        f"  Backend: {backend_result['reason']!r}"
    )


# ===========================================================================
# classify_risk parity
# ===========================================================================

@_skip_no_backend
class TestClassifyRiskParity:
    def test_empty_action(self):
        _assert_classify_parity("tool_call", "get_data", {}, "agent")

    def test_delete_action_type(self):
        _assert_classify_parity("delete", "delete_record", {}, "agent")

    def test_delete_in_action_name(self):
        _assert_classify_parity("tool_call", "delete_old_records", {}, "agent")

    def test_write_action_type(self):
        _assert_classify_parity("write", "update_file", {}, "agent")

    def test_communication_send_email(self):
        _assert_classify_parity("tool_call", "send_email", {"to": "user@example.com"}, "agent")

    def test_communication_message(self):
        _assert_classify_parity("tool_call", "post_message", {}, "agent")

    def test_high_financial_value(self):
        _assert_classify_parity("tool_call", "transfer", {"amount": 50000}, "agent")

    def test_medium_financial_value(self):
        _assert_classify_parity("tool_call", "pay_invoice", {"amount": 2500}, "agent")

    def test_low_financial_value_no_category(self):
        _assert_classify_parity("tool_call", "pay_invoice", {"amount": 50}, "agent")

    def test_sensitive_field_api_key(self):
        _assert_classify_parity("tool_call", "store_config", {"api_key": "sk-abc"}, "agent")

    def test_sensitive_field_password(self):
        _assert_classify_parity("tool_call", "set_creds", {"password": "hunter2"}, "agent")

    def test_iam_action(self):
        _assert_classify_parity("tool_call", "update_iam_role", {}, "agent")

    def test_permission_action(self):
        _assert_classify_parity("tool_call", "grant_permission", {}, "agent")

    def test_schema_migration(self):
        _assert_classify_parity("tool_call", "run_schema_migration", {}, "agent")

    def test_high_risk_agent(self):
        # Backend classify_risk no longer scores high_risk_agents — that gate
        # lives in evaluate_policy only. Both sides produce unregistered_agent
        # when the agent is absent from registered_agents.
        _assert_classify_parity(
            "tool_call", "get_data", {}, "risky-bot",
            chain_context={"high_risk_agents": ["risky-bot"], "registered_agents": {}},
        )

    def test_url_in_payload_exfiltration(self):
        _assert_classify_parity("tool_call", "fetch_url", {"url": "https://ext.com"}, "agent")

    def test_domain_in_payload_exfiltration(self):
        _assert_classify_parity("tool_call", "query", {"domain": "ext.com"}, "agent")

    def test_combined_delete_and_financial(self):
        _assert_classify_parity("delete", "delete_invoice", {"amount": 99999}, "agent")

    def test_combined_write_and_communication(self):
        _assert_classify_parity("write", "send_email", {}, "agent")

    def test_unicode_action_name(self):
        _assert_classify_parity("tool_call", "données_export", {}, "agent")

    def test_very_large_amount(self):
        _assert_classify_parity("tool_call", "transfer", {"amount": 1_000_000}, "agent")


# ===========================================================================
# evaluate_policy parity
# ===========================================================================

@_skip_no_backend
class TestEvaluatePolicyParity:
    # --- Hard deny paths ---

    def test_hard_deny_production_delete(self):
        risk = {"categories": [], "risk_score": 0, "reasons": []}
        _assert_evaluate_parity(
            "tool_call", "delete_record", {}, "agent", risk, {},
            {"environment": "production"},
        )

    def test_hard_deny_cumulative_over_50k(self):
        risk = {"categories": [], "risk_score": 0, "reasons": []}
        metrics = {"financial_exposure_usd": 51_000.0}
        _assert_evaluate_parity("tool_call", "pay", {}, "agent", risk, metrics, {})

    def test_hard_deny_credential_exposure(self):
        risk = {"categories": ["credential_exposure"], "risk_score": 50, "reasons": []}
        _assert_evaluate_parity("tool_call", "store", {}, "agent", risk, {}, {})

    def test_hard_deny_iam(self):
        risk = {"categories": [], "risk_score": 0, "reasons": []}
        _assert_evaluate_parity("tool_call", "update_iam_policy", {}, "agent", risk, {}, {})

    # --- Approval triggers ---

    def test_approval_single_transaction_over_threshold(self):
        risk = {"categories": ["financial_high"], "risk_score": 50, "reasons": []}
        _assert_evaluate_parity(
            "tool_call", "transfer", {"amount": 8000}, "agent", risk, {},
            {"financial_approval_threshold_usd": 5000},
        )

    def test_approval_cumulative_over_threshold(self):
        risk = {"categories": [], "risk_score": 0, "reasons": []}
        metrics = {"financial_exposure_usd": 12_000.0}
        _assert_evaluate_parity(
            "tool_call", "pay", {}, "agent", risk, metrics,
            {"cumulative_threshold_usd": 10_000},
        )

    def test_approval_first_external_communication(self):
        risk = {"categories": ["communication"], "risk_score": 25, "reasons": []}
        metrics = {"external_communications_count": 1}
        _assert_evaluate_parity("tool_call", "send_email", {}, "agent", risk, metrics, {})

    def test_approval_high_risk_score(self):
        risk = {"categories": [], "risk_score": 80, "reasons": []}
        _assert_evaluate_parity("tool_call", "scan", {}, "agent", risk, {}, {})

    def test_approval_high_risk_agent(self):
        risk = {"categories": [], "risk_score": 0, "reasons": []}
        _assert_evaluate_parity(
            "tool_call", "read", {}, "risky-bot", risk, {},
            {"high_risk_agents": ["risky-bot"]},
        )

    # --- Flag paths ---

    def test_flag_write_category(self):
        risk = {"categories": ["write"], "risk_score": 20, "reasons": []}
        _assert_evaluate_parity("write", "update_row", {}, "agent", risk, {}, {})

    def test_flag_medium_risk_score(self):
        risk = {"categories": [], "risk_score": 55, "reasons": []}
        _assert_evaluate_parity("tool_call", "action", {}, "agent", risk, {}, {})

    # --- Default allow paths ---

    def test_default_allow_empty_payload(self):
        risk = {"categories": [], "risk_score": 0, "reasons": []}
        _assert_evaluate_parity("tool_call", "get_record", {}, "agent", risk, {}, {})

    def test_default_allow_low_value_transaction(self):
        risk = {"categories": [], "risk_score": 0, "reasons": []}
        # amount below both thresholds; no categories
        _assert_evaluate_parity(
            "tool_call", "tip", {"amount": 10}, "agent", risk, {},
            {"financial_approval_threshold_usd": 5000, "cumulative_threshold_usd": 10000},
        )

    def test_default_allow_internal_tool_call(self):
        risk = {"categories": [], "risk_score": 5, "reasons": []}
        _assert_evaluate_parity("tool_call", "get_config", {}, "agent", risk, {}, {})

    # --- Mixed-category edge cases ---

    def test_write_plus_financial_write_wins(self):
        # write category is present; financial amount below single threshold
        # write flag fires before risk-score check
        risk = {"categories": ["write", "financial"], "risk_score": 50, "reasons": []}
        _assert_evaluate_parity(
            "write", "update_balance", {"amount": 2000}, "agent", risk, {},
            {"financial_approval_threshold_usd": 5000},
        )

    def test_delete_in_non_production_no_hard_deny(self):
        # "delete" in action name but environment != production → not a hard deny
        risk = {"categories": ["destructive"], "risk_score": 40, "reasons": []}
        _assert_evaluate_parity(
            "tool_call", "delete_temp", {}, "agent", risk, {},
            {"environment": "development"},
        )

    def test_cumulative_exactly_at_threshold_no_deny(self):
        # Exactly AT 50k is not > 50k → no hard deny
        risk = {"categories": [], "risk_score": 0, "reasons": []}
        metrics = {"financial_exposure_usd": 50_000.0}
        _assert_evaluate_parity("tool_call", "pay", {}, "agent", risk, metrics, {})

    def test_second_external_communication_not_approval(self):
        # count == 2 → gate only fires on count == 1
        risk = {"categories": ["communication"], "risk_score": 25, "reasons": []}
        metrics = {"external_communications_count": 2}
        _assert_evaluate_parity("tool_call", "send_email", {}, "agent", risk, metrics, {})

    def test_risk_score_exactly_69_no_approval(self):
        # risk_score == 69 → < 70 → no approval; risk_score >= 40 → flag
        risk = {"categories": [], "risk_score": 69, "reasons": []}
        _assert_evaluate_parity("tool_call", "action", {}, "agent", risk, {}, {})

    def test_risk_score_exactly_70_approval(self):
        risk = {"categories": [], "risk_score": 70, "reasons": []}
        _assert_evaluate_parity("tool_call", "action", {}, "agent", risk, {}, {})

    def test_risk_score_exactly_39_no_flag(self):
        # risk_score == 39 → < 40 → allow (no write category either)
        risk = {"categories": [], "risk_score": 39, "reasons": []}
        _assert_evaluate_parity("tool_call", "action", {}, "agent", risk, {}, {})

    def test_risk_score_exactly_40_flag(self):
        risk = {"categories": [], "risk_score": 40, "reasons": []}
        _assert_evaluate_parity("tool_call", "action", {}, "agent", risk, {}, {})
