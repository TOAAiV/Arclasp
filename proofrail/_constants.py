"""
proofrail._constants — Shared constants used across sanitization, models, and policies.

Kept in their own module to avoid the circular import that would result from
sanitization.py importing ChainConfig from models.py while models.py imports
field-pattern defaults from sanitization.py.

Import from here rather than copying literals into multiple places.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Sensitive-pattern constants — v2 spec section 12
# ---------------------------------------------------------------------------

#: Field-name substrings that trigger redaction of a dict value, regardless
#: of what that value contains.  Case-insensitive substring match is applied
#: against each key.  Extend via ``ChainConfig(sensitive_field_patterns=[...])``.
DEFAULT_SENSITIVE_FIELD_PATTERNS: list[str] = [
    "api_key",
    "password",
    "secret",
    "token",
    "credit_card",
    "ssn",
    "private_key",
]

#: Maximum length for ``action_name`` values recorded on ProofRail events.
#: Adapter layers truncate framework-supplied names to this limit at extraction
#: time before passing them to ``record_agent_action()``.  Suffixes like
#: ``:result`` (7 chars) are appended after truncation, so the stored name is
#: sliced to ``_ACTION_NAME_MAX - len(suffix)`` to keep the final string ≤ 100.
_ACTION_NAME_MAX: int = 100

#: String value prefixes that trigger redaction regardless of the containing
#: key name.  Catches well-known API-key formats embedded as plain values
#: (e.g. ``{"note": "my key is sk_live_abc..."}``) that a key-name scan would
#: miss entirely.  Extend via ``ChainConfig(sensitive_value_patterns=[...])``.
DEFAULT_SENSITIVE_VALUE_PATTERNS: list[str] = [
    "sk_",    # OpenAI / Stripe secret keys
    "pk_",    # Stripe public keys (still sensitive in payload context)
    "ghp_",   # GitHub personal access tokens
    "hf_",    # Hugging Face tokens
    "eyJ",    # JWT tokens — base64url encoding of '{"' — every JWT starts with this
    "AKIA",   # AWS access key IDs (AKIA + 16 uppercase alphanumeric chars)
    "prail_", # ProofRail API keys
]
