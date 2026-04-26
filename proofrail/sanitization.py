"""
proofrail.sanitization — Input and output sanitization utilities.

Sanitization runs on every action payload before it is sent to the backend,
ensuring that secrets and excessively large strings are never transmitted.
"""

from __future__ import annotations

from proofrail.models import ChainConfig


def sanitize_payload(payload: dict, config: ChainConfig) -> dict:
    """
    Recursively sanitize *payload* according to *config* rules:

    1. Any dict key whose name matches (case-insensitive substring) one of
       ``config.sensitive_field_patterns`` has its value replaced with
       ``"[REDACTED]"``.
    2. Any string value longer than ``config.max_payload_string_length`` is
       truncated to that length with ``"...[truncated]"`` appended.
    3. Nested dicts and lists are processed recursively.

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


def _sanitize_value(value: object, config: ChainConfig) -> object:
    if isinstance(value, dict):
        return _sanitize_dict(value, config)
    if isinstance(value, list):
        return [_sanitize_value(item, config) for item in value]
    if isinstance(value, str):
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
