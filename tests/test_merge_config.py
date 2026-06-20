"""
Tests for _merge_config helper — Item F verification.
"""



from proofrail._utils import _merge_config


class TestMergeConfigImportable:
    def test_importable_from_utils(self):
        assert callable(_merge_config)

    def test_langgraph_uses_shared_function(self):
        from proofrail.langgraph.adapter import _merge_config as lg_merge
        assert lg_merge is _merge_config

    def test_langchain_uses_shared_function(self):
        from proofrail.langchain.adapter import _merge_config as lc_merge
        assert lc_merge is _merge_config


class TestMergeConfigBehaviour:
    def test_merges_non_callback_keys(self):
        result = _merge_config({"a": 1}, {"b": 2})
        assert result == {"a": 1, "b": 2}

    def test_extras_override_base_for_non_callback(self):
        result = _merge_config({"key": "old"}, {"key": "new"})
        assert result["key"] == "new"

    def test_none_base_treated_as_empty(self):
        result = _merge_config(None, {"x": 42})
        assert result == {"x": 42}

    def test_empty_extras(self):
        result = _merge_config({"a": 1}, {})
        assert result == {"a": 1}

    def test_callbacks_concatenated_not_overwritten(self):
        cb1, cb2, cb3 = object(), object(), object()
        base = {"callbacks": [cb1, cb2]}
        extras = {"callbacks": [cb3]}
        result = _merge_config(base, extras)
        assert result["callbacks"] == [cb1, cb2, cb3]

    def test_callbacks_from_extras_only(self):
        cb = object()
        result = _merge_config({}, {"callbacks": [cb]})
        assert result["callbacks"] == [cb]

    def test_callbacks_from_base_only(self):
        cb = object()
        result = _merge_config({"callbacks": [cb]}, {"other": "val"})
        assert result["callbacks"] == [cb]

    def test_does_not_mutate_base(self):
        base = {"callbacks": [1, 2]}
        _merge_config(base, {"callbacks": [3]})
        assert base["callbacks"] == [1, 2]

    def test_returns_new_dict(self):
        base = {"a": 1}
        result = _merge_config(base, {"b": 2})
        assert result is not base
