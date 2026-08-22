"""
sdk/tests/test_sanitization_defaults.py — Verify sanitization pattern completeness.

Covers:
- All 7 v2-spec field-name patterns are present in DEFAULT_SENSITIVE_FIELD_PATTERNS.
- All 4 value-prefix patterns are present in DEFAULT_SENSITIVE_VALUE_PATTERNS.
- Each of the three previously-missing field patterns (credit_card, ssn, private_key)
  is correctly redacted when a key matches.
- Value-level patterns redact arbitrary string values (sk_, ghp_, etc.) regardless
  of the containing key name.
- Nested dict and list payloads are handled recursively.
- policies._SENSITIVE_PATTERNS and sanitization.DEFAULT_SENSITIVE_FIELD_PATTERNS
  contain the same members (drift regression guard).
- Chain.metadata is sanitized before the ChainCreate POST (SDK-S-1 regression).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import arclasp
from arclasp._constants import (
    DEFAULT_SENSITIVE_FIELD_PATTERNS,
    DEFAULT_SENSITIVE_VALUE_PATTERNS,
)
from arclasp.sanitization import sanitize_payload
from arclasp.models import ChainConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _config(**overrides) -> ChainConfig:
    """Return a minimal ChainConfig suitable for sanitization tests."""
    defaults = dict(api_key="prail_test", backend_url="http://localhost:9999")
    return ChainConfig(**{**defaults, **overrides})


# ---------------------------------------------------------------------------
# 1. Default pattern completeness
# ---------------------------------------------------------------------------

def test_default_sensitive_field_patterns_complete():
    """All required field-name patterns must be present.

    Uses ``"_token"`` (with underscore) rather than plain ``"token"`` so that
    LLM telemetry counters (``input_tokens``, ``output_tokens``) are NOT
    redacted.  A bare key named exactly ``"token"`` is still caught by
    ``_is_sensitive``'s exact-name guard in ``sanitization.py``.
    """
    required = {"api_key", "password", "secret", "_token", "credit_card", "ssn", "private_key"}
    actual = set(DEFAULT_SENSITIVE_FIELD_PATTERNS)
    missing = required - actual
    assert not missing, f"Missing required field patterns: {missing}"


def test_default_sensitive_value_patterns_complete():
    """All 7 value-prefix patterns must be present (4 original + 3 added in SDK-S-8)."""
    required = {"sk_", "pk_", "ghp_", "hf_", "eyJ", "AKIA", "prail_"}
    actual = set(DEFAULT_SENSITIVE_VALUE_PATTERNS)
    missing = required - actual
    assert not missing, f"Missing required value patterns: {missing}"


# ---------------------------------------------------------------------------
# 2. Previously-missing field patterns now redacted
# ---------------------------------------------------------------------------

def test_credit_card_field_redacted():
    cfg = _config()
    result = sanitize_payload({"credit_card": "4111-1111-1111-1111"}, cfg)
    assert result["credit_card"] == "[REDACTED]"


def test_ssn_field_redacted():
    cfg = _config()
    result = sanitize_payload({"ssn": "123-45-6789"}, cfg)
    assert result["ssn"] == "[REDACTED]"


def test_private_key_field_redacted():
    cfg = _config()
    result = sanitize_payload({"private_key": "-----BEGIN RSA PRIVATE KEY-----"}, cfg)
    assert result["private_key"] == "[REDACTED]"


def test_token_field_redacted():
    """A bare 'token' key must be redacted (exact-name guard)."""
    cfg = _config()
    result = sanitize_payload({"token": "some-bearer-token"}, cfg)
    assert result["token"] == "[REDACTED]"


def test_token_suffix_fields_redacted():
    """Fields ending in _token (auth_token, access_token) must be redacted."""
    cfg = _config()
    result = sanitize_payload({"auth_token": "abc", "access_token": "xyz"}, cfg)
    assert result["auth_token"] == "[REDACTED]"
    assert result["access_token"] == "[REDACTED]"


def test_llm_token_counters_not_redacted():
    """input_tokens, output_tokens, total_tokens must NOT be redacted — LLM telemetry."""
    cfg = _config()
    result = sanitize_payload({"input_tokens": 42, "output_tokens": 15, "total_tokens": 57}, cfg)
    assert result["input_tokens"] == 42, "input_tokens must not be redacted"
    assert result["output_tokens"] == 15, "output_tokens must not be redacted"
    assert result["total_tokens"] == 57, "total_tokens must not be redacted"


# ---------------------------------------------------------------------------
# 3. Value-level prefix patterns
# ---------------------------------------------------------------------------

def test_sk_prefix_value_redacted_regardless_of_key():
    """sk_ values are redacted even when the key name is innocuous."""
    cfg = _config()
    result = sanitize_payload({"data": "sk_test_abcd1234"}, cfg)
    assert result["data"] == "[REDACTED]"


def test_non_sensitive_value_not_redacted():
    """Plain string values with no sensitive prefix pass through."""
    cfg = _config()
    result = sanitize_payload({"note": "some random text"}, cfg)
    assert result["note"] == "some random text"


def test_ghp_prefix_value_redacted():
    """GitHub personal access tokens embedded in values are redacted."""
    cfg = _config()
    result = sanitize_payload({"auth": "ghp_abc123XYZ"}, cfg)
    assert result["auth"] == "[REDACTED]"


def test_hf_prefix_value_redacted():
    """Hugging Face tokens embedded in values are redacted."""
    cfg = _config()
    result = sanitize_payload({"model_key": "hf_SuperSecretToken"}, cfg)
    assert result["model_key"] == "[REDACTED]"


def test_pk_prefix_value_redacted():
    """pk_ values (Stripe public keys) are redacted in payload context."""
    cfg = _config()
    result = sanitize_payload({"payment_key": "pk_live_abc123"}, cfg)
    assert result["payment_key"] == "[REDACTED]"


# ---------------------------------------------------------------------------
# 4. Recursive handling
# ---------------------------------------------------------------------------

def test_recursive_sanitization_handles_nested_dicts():
    """Field-name and value-level redaction both apply inside nested dicts."""
    cfg = _config()
    payload = {"level1": {"level2": {"api_key": "sk_secret_value"}}}
    result = sanitize_payload(payload, cfg)
    # api_key field-name triggers redaction
    assert result["level1"]["level2"]["api_key"] == "[REDACTED]"


def test_recursive_sanitization_handles_lists():
    """Value-level patterns are applied to strings inside lists."""
    cfg = _config()
    payload = {"keys": ["sk_one", "sk_two", "harmless"]}
    result = sanitize_payload(payload, cfg)
    assert result["keys"][0] == "[REDACTED]"
    assert result["keys"][1] == "[REDACTED]"
    assert result["keys"][2] == "harmless"


# ---------------------------------------------------------------------------
# 5. Drift regression guard — policies._SENSITIVE_PATTERNS must match
# ---------------------------------------------------------------------------

def test_policies_engine_uses_same_constants():
    """
    policies._SENSITIVE_PATTERNS must contain exactly the same members as
    sanitization.DEFAULT_SENSITIVE_FIELD_PATTERNS.

    This test fails if someone adds a pattern to one place and forgets the
    other — catching the exact drift documented in audit finding M-7.
    """
    from arclasp.policies import _SENSITIVE_PATTERNS
    assert set(_SENSITIVE_PATTERNS) == set(DEFAULT_SENSITIVE_FIELD_PATTERNS), (
        "Drift detected between policies._SENSITIVE_PATTERNS and "
        "sanitization.DEFAULT_SENSITIVE_FIELD_PATTERNS. "
        "Update arclasp/_constants.py — both sets derive from there."
    )


# ---------------------------------------------------------------------------
# 6. api_key uses SecretStr — repr masks value, get_secret_value() works (SDK-S-7)
# ---------------------------------------------------------------------------

def test_chain_config_repr_masks_api_key():
    """repr(ChainConfig) must not expose the api_key value."""
    cfg = ChainConfig(api_key="prail_sk_supersecret", backend_url="http://localhost:9999")
    r = repr(cfg)
    assert "prail_sk_supersecret" not in r, "api_key value must not appear in repr()"
    assert "api_key" in r, "api_key field name must still be visible in repr()"


def test_chain_config_get_secret_value_returns_actual_key():
    """The actual key value must be retrievable via .get_secret_value() for HTTP headers."""
    cfg = ChainConfig(api_key="prail_sk_supersecret", backend_url="http://localhost:9999")
    assert cfg.api_key.get_secret_value() == "prail_sk_supersecret"


# ---------------------------------------------------------------------------
# 7. Chain.metadata sanitized before ChainCreate POST (SDK-S-1 regression)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chain_metadata_api_key_redacted_in_post():
    """
    Field-name path: api_key in Chain metadata must arrive at the backend as
    [REDACTED], not the raw value.  Non-sensitive fields pass through unchanged.
    """
    from arclasp.chain import Chain

    captured: dict = {}

    async def mock_post(path, body, action_type=None, headers=None):
        if path == "/v1/chains":
            captured["body"] = body
            return {"id": "chain-sec-001"}
        if path.endswith("/complete"):
            return {"status": "completed"}
        return {
            "policy_decision": "allow",
            "decision_reason": "",
            "decision_source": "backend_evaluation",
        }

    arclasp.init(api_key="prail_test", backend_url="http://localhost:9999")
    with patch("arclasp.client._post", side_effect=mock_post):
        async with Chain("test", metadata={"api_key": "sk_live_abc123", "order_id": "999"}):
            pass

    meta = captured["body"]["metadata"]
    assert meta["api_key"] == "[REDACTED]", "api_key field must be redacted"
    assert meta["order_id"] == "999", "non-sensitive field must pass through unchanged"


@pytest.mark.asyncio
async def test_chain_metadata_value_prefix_redacted_in_post():
    """
    Value-prefix path: a GitHub token stored under a benign key name (github_token)
    must be redacted because the value starts with 'ghp_', regardless of the key.
    """
    from arclasp.chain import Chain

    captured: dict = {}

    async def mock_post(path, body, action_type=None, headers=None):
        if path == "/v1/chains":
            captured["body"] = body
            return {"id": "chain-sec-002"}
        if path.endswith("/complete"):
            return {"status": "completed"}
        return {
            "policy_decision": "allow",
            "decision_reason": "",
            "decision_source": "backend_evaluation",
        }

    arclasp.init(api_key="prail_test", backend_url="http://localhost:9999")
    with patch("arclasp.client._post", side_effect=mock_post):
        async with Chain(
            "test",
            metadata={"github_token": "ghp_abc123XYZ", "env": "staging"},
        ):
            pass

    meta = captured["body"]["metadata"]
    assert meta["github_token"] == "[REDACTED]", "ghp_ value must be redacted by value-prefix path"
    assert meta["env"] == "staging", "non-sensitive field must pass through unchanged"


@pytest.mark.asyncio
async def test_chain_metadata_nested_dict_sanitized():
    """
    Recursive path: sensitive data nested inside a sub-dict within metadata must
    still be redacted.  Verifies that sanitize_payload's recursive traversal
    applies through the metadata channel.
    """
    from arclasp.chain import Chain

    captured: dict = {}

    async def mock_post(path, body, action_type=None, headers=None):
        if path == "/v1/chains":
            captured["body"] = body
            return {"id": "chain-sec-003"}
        if path.endswith("/complete"):
            return {"status": "completed"}
        return {
            "policy_decision": "allow",
            "decision_reason": "",
            "decision_source": "backend_evaluation",
        }

    arclasp.init(api_key="prail_test", backend_url="http://localhost:9999")
    with patch("arclasp.client._post", side_effect=mock_post):
        async with Chain(
            "test",
            metadata={"customer": {"name": "Acme", "secret_key": "sk_live_xyz"}},
        ):
            pass

    meta = captured["body"]["metadata"]
    assert meta["customer"]["name"] == "Acme", "non-sensitive nested field must pass through"
    assert meta["customer"]["secret_key"] == "[REDACTED]", "nested secret_key must be redacted"


# ---------------------------------------------------------------------------
# 8. Expanded value-prefix patterns: JWT, AWS access keys, Arclasp keys
#    (SDK-S-8 regression)
# ---------------------------------------------------------------------------

def test_jwt_prefix_value_redacted():
    """eyJ prefix matches all JWTs — base64url encoding of '{"' that starts every JWT."""
    cfg = _config()
    result = sanitize_payload({"auth": "eyJhbGciOiJIUzI1NiJ9.payload.sig"}, cfg)
    assert result["auth"] == "[REDACTED]"


def test_aws_access_key_prefix_redacted():
    """AKIA prefix matches AWS access key IDs embedded as plain values."""
    cfg = _config()
    result = sanitize_payload({"aws_id": "AKIAIOSFODNN7EXAMPLE"}, cfg)
    assert result["aws_id"] == "[REDACTED]"


def test_arclasp_key_prefix_redacted():
    """prail_ prefix redacts Arclasp API keys appearing in customer payloads."""
    cfg = _config()
    result = sanitize_payload({"service_key": "prail_sk_live_abc123"}, cfg)
    assert result["service_key"] == "[REDACTED]"


# ---------------------------------------------------------------------------
# 9. bytes redaction (SDK-S-9 regression)
# ---------------------------------------------------------------------------

def test_sanitize_bytes_value_redacted():
    """bytes values are replaced with [REDACTED_BYTES] — binary may encode credentials."""
    cfg = _config()
    result = sanitize_payload({"cert": b"\x89PNG\r\n\x1a\n"}, cfg)
    assert result["cert"] == "[REDACTED_BYTES]"


def test_sanitize_bytes_in_nested_dict():
    """bytes values nested inside a sub-dict are redacted via recursive traversal."""
    cfg = _config()
    result = sanitize_payload({"config": {"tls_cert": b"-----BEGIN CERT-----"}}, cfg)
    assert result["config"]["tls_cert"] == "[REDACTED_BYTES]"
