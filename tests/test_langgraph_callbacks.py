"""
Tests for arclasp.langgraph.callbacks._state_to_dict.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from arclasp.langgraph.callbacks import _state_to_dict


def test_state_to_dict_normalizes_nonjson_values_recursively():
    nested = SimpleNamespace(role="user", content="hello")
    state = {"messages": {"human": nested}, "step": 1}

    result = _state_to_dict(state)

    assert json.dumps(result)


def test_state_to_dict_handles_lists_of_nonjson_objects():
    nested = SimpleNamespace(role="assistant", content="hi there")
    state = {"messages": [nested, "plain string"]}

    result = _state_to_dict(state)

    assert isinstance(result["messages"], list)
    assert json.dumps(result)
