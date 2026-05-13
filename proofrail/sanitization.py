"""
proofrail.sanitization — Input and output sanitization utilities.

Sanitization runs on every action payload before it is sent to the backend,
ensuring that secrets and excessively large strings are never transmitted.

Two categories of redaction (v2 spec section 12):

1. Field-name patterns  — any dict key that is a case-insensitive substring
   match against ``DEFAULT_SENSITIVE_FIELD_PATTERNS`` (or the caller-supplied
   ``config.sensitive_field_patterns``) has its value replaced with
   ``"[REDACTED]"``.

2. Value-prefix patterns — any string value that starts with one of the
   ``DEFAULT_SENSITIVE_VALUE_PATTERNS`` prefixes (or
   ``config.sensitive_value_patterns``) is replaced with ``"[REDACTED]"``
   regardless of the key name.  This catches leaked API keys that appear as
   plain string values (e.g. ``{"text": "my key is sk_live_abc..."}``)
"""

from __future__ import annotations

# Re-export the shared constants so callers can do:
#   from proofrail.sanitization import DEFAULT_SENSITIVE_FIELD_PATTERNS
# The definitions live in _constants.py to avoid the circular import that would
# arise if models.py imported from here while this module imports ChainConfig
# from models.py.
from proofrail._constants import (
    DEFAULT_SENSITIVE_FIELD_PATTERNS,
    DEFAULT_SENSITIVE_VALUE_PATTERNS,
)

from proofrail.models import ChainConfig


def sanitize_payload(payload: dict, config: ChainConfig) -> dict:
    """
    Recursively sanitize *payload* according to *config* rules:

    1. Any dict key whose name matches (case-insensitive substring) one of
       ``config.sensitive_field_patterns`` has its value replaced with
       ``"[REDACTED]"``.
    2. Any string value that starts with a prefix in
       ``config.sensitive_value_patterns`` is replaced with ``"[REDACTED]"``
       regardless of its key name (catches leaked API keys in values).
    3. Any string value longer than ``config.max_payload_string_length`` is
       truncated to that length with ``"...[truncated]"`` appended.
    4. Nested dicts and lists are processed recursively.

    The original payload is not mutated — a new dict is returned.
    """
    return _sanitize_value(payload, config)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_sensitive(key: str, patterns: list[str]) -> bool:
    key_lower = key.lower()
    return any(pattern.lower() in key_lower for pattern in patterns)


def _truncate(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    return value[:max_length] + "...[truncated]"


def _has_sensitive_value_prefix(value: str, patterns: list[str]) -> bool:
    for prefix in patterns:
        if value.startswith(prefix):
            return True
    return False


def _sanitize_value(value: object, config: ChainConfig) -> object:
    if isinstance(value, dict):
        return _sanitize_dict(value, config)
    if isinstance(value, list):
        return [_sanitize_value(item, config) for item in value]
    if isinstance(value, str):
        # Value-level redaction: catch API keys embedded as string values
        # (e.g. {"text": "sk_live_abc..."}) regardless of the key name.
        if _has_sensitive_value_prefix(value, config.sensitive_value_patterns):
            return "[REDACTED]"
        return _truncate(value, config.max_payload_string_length)
    return value


def _sanitize_dict(payload: dict, config: ChainConfig) -> dict:
    result: dict = {}
    for key, value in payload.items():
        if _is_sensitive(key, config.sensitive_field_patterns):
            result[key] = "[REDACTED]"
        else:
            result[key] = _sanitize_value(value, config)
    return result
