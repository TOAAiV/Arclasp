"""
Tests for proofrail.fast_path — is_fast_path_eligible and evaluate_fast_path.
"""

from __future__ import annotations

import time

import pytest

import proofrail
from proofrail.fast_path import evaluate_fast_path, is_fast_path_eligible
from proofrail.models import ChainConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _config(**kwargs) -> ChainConfig:
    defaults = dict(
        api_key="prail_test",
        environment="development",
        enable_local_fast_path=True,
        cumulative_financial_threshold_usd=10_000.0,
        financial_approval_threshold_usd=5_000.0,
        high_risk_agents=[],
    )
    defaults.update(kwargs)
    return ChainConfig(**defaults)


def _eligible(action_type="tool_call", action_name="get_record", payload=None,
              agent_name="agent", metrics=None, config=None):
    return is_fast_path_eligible(
        action_type, action_name, payload or {}, agent_name,
        metrics or {}, config or _config(),
    )


def _evaluate(action_type="tool_call", action_name="get_record", payload=None,
              agent_name="agent", metrics=None, config=None):
    return evaluate_fast_path(
        action_type, action_name, payload or {}, agent_name,
        metrics or {}, config or _config(),
    )


# ===========================================================================
# is_fast_path_eligible
# ===========================================================================

class TestIsEligible:
    def test_simple_read_action_eligible(self):
        eligible, reason = _eligible()
        assert eligible is True
        assert "passed" in reason

    def test_flag_false_not_eligible(self):
        eligible, reason = _eligible(config=_config(enable_local_fast_path=False))
        assert eligible is False
        assert "enable_local_fast_path" in reason

    def test_production_not_eligible(self):
        eligible, reason = _eligible(config=_config(environment="production"))
        assert eligible is False
        assert "production" in reason

    def test_high_risk_score_not_eligible(self):
        # delete action → risk_score = 40 → not eligible
        eligible, reason = _eligible(action_type="delete", action_name="delete_record")
        assert eligible is False
        assert "risk score" in reason

    def test_blocking_category_destructive_not_eligible(self):
        eligible, reason = _eligible(action_type="delete", action_name="remove_thing")
        assert eligible is False

    def test_blocking_category_credential_exposure_not_eligible(self):
        eligible, reason = _eligible(payload={"api_key": "sk-abc"})
        assert eligible is False
        # credential_exposure adds +50 risk score, so the risk-score gate fires
        # before the category gate — either reason indicates correctly blocked
        assert "risk score" in reason or "blocking categories" in reason

    def test_financial_near_threshold_not_eligible(self):
        # 80% of 10000 = 8000; current = 8500 → not eligible
        metrics = {"financial_exposure_usd": 8500.0}
        eligible, reason = _eligible(metrics=metrics)
        assert eligible is False
        assert "financial exposure" in reason

    def test_financial_just_below_threshold_eligible(self):
        # 79% of 10000 = 7900 → eligible
        metrics = {"financial_exposure_usd": 7999.0}
        eligible, reason = _eligible(metrics=metrics)
        assert eligible is True

    def test_high_risk_agent_not_eligible(self):
        eligible, reason = _eligible(
            agent_name="risky-bot",
            config=_config(high_risk_agents=["risky-bot"]),
        )
        assert eligible is False
        assert "high_risk_agents" in reason

    def test_risk_score_39_eligible(self):
        # IAM adds +40; let's use a low-risk action
        eligible, reason = _eligible(action_name="list_items")
        assert eligible is True

    def test_financial_exactly_at_80_percent_not_eligible(self):
        # Exactly at 80% boundary: 8000 >= 8000 → not eligible
        metrics = {"financial_exposure_usd": 8000.0}
        eligible, reason = _eligible(metrics=metrics)
        assert eligible is False


# ===========================================================================
# evaluate_fast_path — decision
# ===========================================================================

class TestEvaluateFastPath:
    def test_eligible_action_returns_allow_dict(self):
        result = _evaluate()
        assert result is not None
        assert result["policy_decision"] == "allow"
        assert result["decision_source"] == "local_fast_path"
        assert "decision_reason" in result

    def test_ineligible_returns_none(self):
        result = _evaluate(config=_config(enable_local_fast_path=False))
        assert result is None

    def test_production_returns_none(self):
        result = _evaluate(config=_config(environment="production"))
        assert result is None

    def test_write_action_eligible_but_local_policy_flags_returns_none(self):
        # write action: risk_score = 20 (<40, eligible by criteria 3)
        # but "write" category is not in blocking set, so risk passes.
        # However evaluate_policy returns allow_with_flag for write category.
        # → evaluate_fast_path should return None (policy didn't say allow)
        result = _evaluate(action_type="write", action_name="update_record")
        assert result is None

    def test_result_is_policy_decision_parseable(self):
        from proofrail.models import PolicyDecision
        result = _evaluate()
        assert result is not None
        pd = PolicyDecision.model_validate(result)
        assert pd.policy_decision == "allow"
        assert pd.decision_source == "local_fast_path"


# ===========================================================================
# Part 3 — ChainConfig flag test (enable_local_fast_path wiring)
# ===========================================================================

class TestChainConfigFlagWiring:
    def test_flag_defaults_to_true(self):
        cfg = ChainConfig(api_key="test")
        assert cfg.enable_local_fast_path is True

    def test_flag_false_makes_evaluate_return_none(self):
        cfg = _config(enable_local_fast_path=False)
        result = evaluate_fast_path("tool_call", "get_data", {}, "agent", {}, cfg)
        assert result is None

    def test_flag_true_makes_evaluate_return_decision(self):
        cfg = _config(enable_local_fast_path=True)
        result = evaluate_fast_path("tool_call", "get_data", {}, "agent", {}, cfg)
        assert result is not None


# ===========================================================================
# Latency sanity check
# ===========================================================================

class TestFastPathLatency:
    def test_1000_calls_under_1_second(self):
        cfg = _config()
        start = time.perf_counter()
        for _ in range(1000):
            evaluate_fast_path("tool_call", "get_data", {}, "agent", {}, cfg)
        elapsed = time.perf_counter() - start
        assert elapsed < 1.0, (
            f"1000 fast-path calls took {elapsed:.3f}s — expected < 1.0s"
        )
