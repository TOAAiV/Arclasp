"""
proofrail._utils — Shared internal helpers.
"""

from __future__ import annotations


def _merge_config(base: dict | None, extras: dict) -> dict:
    """
    Return a new config dict that merges *extras* into *base*.

    The ``"callbacks"`` key is handled specially: if both dicts contain it,
    the lists are concatenated rather than overwritten, so user-provided
    callbacks coexist with framework-injected ones.
    """
    result: dict = dict(base or {})
    for key, value in extras.items():
        if key == "callbacks" and "callbacks" in result:
            existing = list(result["callbacks"])
            result["callbacks"] = existing + list(value)
        else:
            result[key] = value
    return result
