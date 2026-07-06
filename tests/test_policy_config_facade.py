"""
Tests for the per-chain policy_config facade (add_financial_threshold, and
the Chain(policy_config={...}) constructor kwarg).

Backend previously enforced financial thresholds org-wide only. Chains can
now carry an optional policy_config dict, merged by the backend key-by-key
over the org-wide config (chain value wins where set). No new class
hierarchy — plain dicts only, built either directly or via the
add_financial_threshold() facade method.
"""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

import proofrail
from proofrail.chain import Chain


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def sdk_init():
    proofrail.init(
        api_key="prail_test",
        backend_url="http://test",
        fail_mode="deny",
        enable_local_fast_path=False,
        max_retries=1,
        retry_backoff_base_ms=0,
    )


_CHAIN_RESPONSE = {"id": "test-chain-001"}


# ---------------------------------------------------------------------------
# Unit tests — add_financial_threshold() builds the correct dict
# ---------------------------------------------------------------------------

class TestAddFinancialThreshold:

    def test_usd_only(self):
        chain = Chain("test")
        chain.add_financial_threshold(usd=10_000)
        assert chain.policy_config == {"cumulative_financial_threshold_usd": 10_000}

    def test_usd_with_notify(self):
        chain = Chain("test")
        chain.add_financial_threshold(usd=10_000, notify=["a@example.com", "b@example.com"])
        assert chain.policy_config == {
            "cumulative_financial_threshold_usd": 10_000,
            "notify": ["a@example.com", "b@example.com"],
        }

    def test_notify_deduplicated_against_existing(self):
        chain = Chain("test")
        chain.add_financial_threshold(usd=10_000, notify=["a@example.com"])
        chain.add_financial_threshold(usd=5_000, notify=["a@example.com", "b@example.com"])
        assert chain.policy_config["notify"] == ["a@example.com", "b@example.com"]
        # Second call's usd wins (most recent call sets the threshold)
        assert chain.policy_config["cumulative_financial_threshold_usd"] == 5_000

    def test_deny_true_sets_action(self):
        chain = Chain("test")
        chain.add_financial_threshold(usd=10_000, deny=True)
        assert chain.policy_config == {
            "cumulative_financial_threshold_usd": 10_000,
            "cumulative_financial_threshold_action": "deny",
        }

    def test_deny_false_omits_action_key(self):
        chain = Chain("test")
        chain.add_financial_threshold(usd=10_000, deny=False)
        assert "cumulative_financial_threshold_action" not in chain.policy_config

    def test_no_notify_omits_notify_key(self):
        chain = Chain("test")
        chain.add_financial_threshold(usd=10_000)
        assert "notify" not in chain.policy_config


# ---------------------------------------------------------------------------
# Unit tests — Chain(policy_config={...}) constructor kwarg
# ---------------------------------------------------------------------------

class TestChainConstructorPolicyConfig:

    def test_stores_passed_dict(self):
        cfg = {"cumulative_financial_threshold_usd": 25_000, "notify": ["x@example.com"]}
        chain = Chain("test", policy_config=cfg)
        assert chain.policy_config == cfg

    def test_default_is_empty_dict(self):
        chain = Chain("test")
        assert chain.policy_config == {}

    def test_does_not_alias_caller_dict(self):
        """Chain must copy the dict, not hold a reference to the caller's object."""
        cfg = {"cumulative_financial_threshold_usd": 25_000}
        chain = Chain("test", policy_config=cfg)
        cfg["cumulative_financial_threshold_usd"] = 999_999
        assert chain.policy_config["cumulative_financial_threshold_usd"] == 25_000

    def test_constructor_dict_and_facade_combine(self):
        """add_financial_threshold() on top of a constructor-supplied dict
        overrides/extends rather than replacing it wholesale."""
        chain = Chain("test", policy_config={"notify": ["preset@example.com"]})
        chain.add_financial_threshold(usd=10_000, notify=["added@example.com"])
        assert chain.policy_config["cumulative_financial_threshold_usd"] == 10_000
        assert chain.policy_config["notify"] == ["preset@example.com", "added@example.com"]


# ---------------------------------------------------------------------------
# Integration test — POST /v1/chains body contains policy_config when set
# ---------------------------------------------------------------------------

class TestChainCreationBody:

    @pytest.mark.asyncio
    async def test_policy_config_included_when_set_via_facade(self):
        captured = {}

        async def fake_post(path, data, action_type=None):
            if path == "/v1/chains":
                captured.update(data)
                return _CHAIN_RESPONSE
            return {"policy_decision": "allow"}

        with patch("proofrail.client._post", side_effect=fake_post):
            chain = Chain("test-facade")
            chain.add_financial_threshold(usd=10_000, notify=["ops@example.com"])
            async with chain:
                pass

        assert captured["policy_config"] == {
            "cumulative_financial_threshold_usd": 10_000,
            "notify": ["ops@example.com"],
        }

    @pytest.mark.asyncio
    async def test_policy_config_included_when_set_via_constructor(self):
        captured = {}

        async def fake_post(path, data, action_type=None):
            if path == "/v1/chains":
                captured.update(data)
                return _CHAIN_RESPONSE
            return {"policy_decision": "allow"}

        raw_config = {"cumulative_financial_threshold_usd": 50_000}
        with patch("proofrail.client._post", side_effect=fake_post):
            async with Chain("test-ctor", policy_config=raw_config):
                pass

        assert captured["policy_config"] == raw_config

    @pytest.mark.asyncio
    async def test_policy_config_empty_dict_when_not_set(self):
        """Backward compat: a chain with no override sends {} — the backend
        treats {} identically to NULL (org-wide config applies unchanged)."""
        captured = {}

        async def fake_post(path, data, action_type=None):
            if path == "/v1/chains":
                captured.update(data)
                return _CHAIN_RESPONSE
            return {"policy_decision": "allow"}

        with patch("proofrail.client._post", side_effect=fake_post):
            async with Chain("test-default"):
                pass

        assert captured["policy_config"] == {}

    @pytest.mark.asyncio
    async def test_facade_and_constructor_and_advanced_dict_all_reach_backend_identically(self):
        """
        The three usage patterns (facade, constructor kwarg, and hand-built
        raw dict) must produce byte-identical policy_config bodies for
        equivalent configuration — there is no separate "advanced API" with
        different wire behavior.
        """
        bodies = []

        async def fake_post(path, data, action_type=None):
            if path == "/v1/chains":
                bodies.append(data["policy_config"])
                return _CHAIN_RESPONSE
            return {"policy_decision": "allow"}

        with patch("proofrail.client._post", side_effect=fake_post):
            facade_chain = Chain("via-facade")
            facade_chain.add_financial_threshold(usd=10_000, notify=["a@example.com"])
            async with facade_chain:
                pass

            async with Chain(
                "via-constructor",
                policy_config={
                    "cumulative_financial_threshold_usd": 10_000,
                    "notify": ["a@example.com"],
                },
            ):
                pass

        assert bodies[0] == bodies[1]
