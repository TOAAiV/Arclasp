"""
proofrail._utils — Shared internal helpers.
"""

from __future__ import annotations


def _merge_config(base: dict | None, extras: dict) -> dict:
    """Merge extras into base; concatenate "callbacks" lists rather than overwriting."""
    result: dict = dict(base or {})
    for key, value in extras.items():
        if key == "callbacks" and "callbacks" in result:
            existing = list(result["callbacks"])
            result["callbacks"] = existing + list(value)
        else:
            result[key] = value
    return result
