from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEMOS = [
    ROOT / "demos" / "langchain_minimal.py",
    ROOT / "demos" / "langgraph_minimal.py",
    ROOT / "demos" / "crewai_minimal.py",
    ROOT / "demos" / "mcp_minimal.py",
]


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_replacement_demos_import_without_environment_or_backend_calls(monkeypatch):
    monkeypatch.delenv("ARCLASP_API_KEY", raising=False)
    for path in DEMOS:
        _load_module(path)


def test_replacement_demos_do_not_patch_private_transport():
    forbidden = {"_post", "_get", "patch", "AsyncMock"}
    for path in DEMOS:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        attrs = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        assert forbidden.isdisjoint(names | attrs), path


def test_langgraph_example_uses_real_graph_primitives():
    path = ROOT / "demos" / "langgraph_minimal.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls = {
        getattr(node.func, "id", getattr(node.func, "attr", ""))
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert {"StateGraph", "governed_node", "compile"}.issubset(calls)


def test_examples_require_arclasp_api_key_at_runtime(monkeypatch):
    monkeypatch.delenv("ARCLASP_API_KEY", raising=False)
    for path in DEMOS:
        module = _load_module(path)
        try:
            module._require_api_key()
        except SystemExit as exc:
            assert "ARCLASP_API_KEY" in str(exc)
        else:  # pragma: no cover - assertion branch
            raise AssertionError(f"{path} did not require ARCLASP_API_KEY")
