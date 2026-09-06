"""
arclasp.sanitization — Input and output sanitization utilities.

Sanitization runs on every action payload before it is sent to the backend,
ensuring that secrets and excessively large strings are never transmitted.

Two categories of redaction (v2 spec section 12):

1. Field-name patterns  — any dict key that is a case-insensitive substring
   match against ``DEFAULT_SENSITIVE_FIELD_PATTERNS`` (or the caller-supplied
   ``config.sensitive_field_patterns``) has its value replaced with
   ``"[REDACTED]"``.

2. Value-prefix patterns — any token-like segment in a string value that
   starts with one of the ``DEFAULT_SENSITIVE_VALUE_PATTERNS`` prefixes (or
   ``config.sensitive_value_patterns``) is replaced with ``"[REDACTED]"``
   regardless of the key name. This catches leaked API keys that appear as
   plain string values (e.g. ``{"text": "my key is sk_live_abc..."}``)
"""

from __future__ import annotations

from typing import cast

# Re-export the shared constants so callers can do:
#   from arclasp.sanitization import DEFAULT_SENSITIVE_FIELD_PATTERNS
# The definitions live in _constants.py to avoid the circular import that would
# arise if models.py imported from here while this module imports ChainConfig
# from models.py.
# The `import X as X` form signals to ruff/mypy that these are intentional
# re-exports rather than unused imports.
from arclasp._constants import (
    DEFAULT_SENSITIVE_FIELD_PATTERNS as DEFAULT_SENSITIVE_FIELD_PATTERNS,
    DEFAULT_SENSITIVE_VALUE_PATTERNS as DEFAULT_SENSITIVE_VALUE_PATTERNS,
)
from arclasp.models import ChainConfig

__all__ = [
    "sanitize_payload",
    "DEFAULT_SENSITIVE_FIELD_PATTERNS",
    "DEFAULT_SENSITIVE_VALUE_PATTERNS",
]


def sanitize_payload(payload: dict, config: ChainConfig) -> dict:
    """
    Recursively sanitize *payload* according to *config* rules:

    1. Any dict key whose name matches (case-insensitive substring) one of
       ``config.sensitive_field_patterns`` has its value replaced with
       ``"[REDACTED]"``.
    2. Any token-like segment in a string value that starts with a prefix in
       ``config.sensitive_value_patterns`` is replaced with ``"[REDACTED]"``
       regardless of its key name (catches leaked API keys in values).
    3. Any string value longer than ``config.max_payload_string_length`` is
       truncated to that length with ``"...[truncated]"`` appended.
    4. Nested dicts and lists are processed recursively.
    5. ``bytes`` values are replaced with ``"[REDACTED_BYTES]"`` — binary blobs
       may encode credentials and cannot be safely inspected by prefix patterns.

    The original payload is not mutated — a new dict is returned.
    """
    return cast(dict, _sanitize_value(payload, config))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_sensitive(key: str, patterns: list[str]) -> bool:
    key_lower = key.lower()
    # Exact match for a bare "token" key — common in many APIs.
    if key_lower == "token":
        return True
    # LLM telemetry counters (input_tokens, output_tokens, total_tokens, …)
    # end in "_tokens" (plural) and must NOT be redacted even though they
    # contain "_token" as a substring.
    if key_lower.endswith("_tokens"):
        return False
    return any(pattern.lower() in key_lower for pattern in patterns)


def _truncate(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    return value[:max_length] + "...[truncated]"


_TOKEN_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "_-"
)


def _token_chars_for_prefix(prefix: str) -> frozenset[str]:
    if prefix == "eyJ" or "." in prefix:
        return _TOKEN_CHARS | frozenset(".")
    return _TOKEN_CHARS


def _is_token_boundary(value: str, index: int) -> bool:
    return index == 0 or value[index - 1] not in _TOKEN_CHARS


def _has_sensitive_value_prefix(value: str, patterns: list[str]) -> bool:
    for prefix in patterns:
        if value.startswith(prefix):
            return True
    return False


def _redact_sensitive_value_segments(value: str, patterns: list[str]) -> str:
    result: list[str] = []
    index = 0
    redacted_whole_value = False

    while index < len(value):
        matched_prefix = next(
            (
                prefix
                for prefix in patterns
                if _is_token_boundary(value, index) and value.startswith(prefix, index)
            ),
            None,
        )
        if matched_prefix is None:
            result.append(value[index])
            index += 1
            continue

        token_chars = _token_chars_for_prefix(matched_prefix)
        end = index + len(matched_prefix)
        while end < len(value) and value[end] in token_chars:
            end += 1
        result.append("[REDACTED]")
        redacted_whole_value = index == 0 and end == len(value)
        index = end

    if redacted_whole_value and result == ["[REDACTED]"]:
        return "[REDACTED]"
    return "".join(result)


def _sanitize_value(value: object, config: ChainConfig) -> object:
    if isinstance(value, dict):
        return _sanitize_dict(value, config)
    if isinstance(value, list):
        return [_sanitize_value(item, config) for item in value]
    if isinstance(value, str):
        # Fast compatibility path for whole-value tokens, then segment redaction
        # for token-like credentials embedded in otherwise harmless text.
        if _has_sensitive_value_prefix(value, config.sensitive_value_patterns):
            return "[REDACTED]"
        redacted = _redact_sensitive_value_segments(
            value, config.sensitive_value_patterns
        )
        return _truncate(redacted, config.max_payload_string_length)
    if isinstance(value, bytes):
        return "[REDACTED_BYTES]"
    return value


def _sanitize_dict(payload: dict, config: ChainConfig) -> dict:
    result: dict = {}
    for key, value in payload.items():
        if _is_sensitive(key, config.sensitive_field_patterns):
            result[key] = "[REDACTED]"
        else:
            result[key] = _sanitize_value(value, config)
    return result
