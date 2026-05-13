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
"""

from __future__ import annotations

import pytest

import proofrail
from proofrail._constants import (
    DEFAULT_SENSITIVE_FIELD_PATTERNS,
    DEFAULT_SENSITIVE_VALUE_PATTERNS,
)
from proofrail.sanitization import sanitize_payload
from proofrail.models import ChainConfig


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
    """All 7 v2-spec field-name patterns must be present."""
    required = {"api_key", "password", "secret", "token", "credit_card", "ssn", "private_key"}
    actual = set(DEFAULT_SENSITIVE_FIELD_PATTERNS)
    missing = required - actual
    assert not missing, f"Missing required field patterns: {missing}"


def test_default_sensitive_value_patterns_complete():
    """All 4 v2-spec value-prefix patterns must be present."""
    required = {"sk_", "pk_", "ghp_", "hf_"}
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
    from proofrail.policies import _SENSITIVE_PATTERNS
    assert set(_SENSITIVE_PATTERNS) == set(DEFAULT_SENSITIVE_FIELD_PATTERNS), (
        "Drift detected between policies._SENSITIVE_PATTERNS and "
        "sanitization.DEFAULT_SENSITIVE_FIELD_PATTERNS. "
        "Update proofrail/_constants.py — both sets derive from there."
    )
