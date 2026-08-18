"""
Integration test support for ProofRail framework adapter tests.

IMPORTANT: sys.modules stubs for langchain_core / langgraph / crewai are seeded
at module import time (before any test-file imports) so that the adapters'
deferred `import` checks see the stub packages instead of ImportError.

mcp (1.27.0) is already installed — not mocked.
"""

from __future__ import annotations

# ============================================================
# 1.  Stub classes for langchain_core base types
#     These must be real Python classes so adapters that call
#     super().__init__() or type() on them don't blow up.
# ============================================================

class _StubBaseCallbackHandler:
    """Minimal stub for langchain_core BaseCallbackHandler."""
    pass


class _StubAsyncCallbackHandler(_StubBaseCallbackHandler):
    """Minimal stub for langchain_core AsyncCallbackHandler."""
    pass


# ============================================================
# 2.  Seed sys.modules BEFORE any adapter imports happen
# ============================================================

import importlib.util  # noqa: E402
import sys  # noqa: E402
from types import ModuleType  # noqa: E402


def _has_real_package(name: str) -> bool:
    """True if the package is genuinely installed (not just stubbed in sys.modules)."""
    try:
        spec = importlib.util.find_spec(name)
        return spec is not None and spec.origin is not None
    except (ValueError, ModuleNotFoundError):
        return False


def _make_mod(name: str, **attrs) -> ModuleType:
    m = ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


_lc_base_mod = _make_mod(
    "langchain_core.callbacks.base",
    BaseCallbackHandler=_StubBaseCallbackHandler,
    AsyncCallbackHandler=_StubAsyncCallbackHandler,
)
_lc_cbs_mod = _make_mod(
    "langchain_core.callbacks",
    base=_lc_base_mod,
    BaseCallbackHandler=_StubBaseCallbackHandler,
    AsyncCallbackHandler=_StubAsyncCallbackHandler,
)
_lc_core_mod = _make_mod("langchain_core", callbacks=_lc_cbs_mod)

for _name, _mod in [
    ("langchain_core",                  _lc_core_mod),
    ("langchain_core.callbacks",        _lc_cbs_mod),
    ("langchain_core.callbacks.base",   _lc_base_mod),
    ("langgraph",                       _make_mod("langgraph")),
    ("crewai",                          _make_mod("crewai")),
]:
    if _has_real_package(_name):
        continue  # real package available — don't override with stub
    sys.modules.setdefault(_name, _mod)


# ============================================================
# 3.  Import adapters now (stubs already in sys.modules).
#     Safety-net: if proofrail.langchain.callbacks was already
#     cached with _BaseCallbackHandler=object, patch it so that
#     govern()'s "is object" guard passes.
# ============================================================

import proofrail  # noqa: E402
import proofrail.langchain.callbacks as _lc_cb_mod  # noqa: E402
import proofrail.langchain.adapter as _lc_adapt_mod  # noqa: E402

if _lc_adapt_mod._BaseCallbackHandler is object:
    _lc_adapt_mod._BaseCallbackHandler = _StubBaseCallbackHandler
if _lc_cb_mod._BaseCallbackHandler is object:
    _lc_cb_mod._BaseCallbackHandler = _StubBaseCallbackHandler

from proofrail.exceptions import BackendUnavailableError  # noqa: E402
import asyncio  # noqa: E402
import pytest  # noqa: E402


# ============================================================
# 4.  Test constants & response helpers
# ============================================================

CHAIN_ID = "test-chain-int-001"

ALLOW_RESP = {
    "policy_decision":  "allow",
    "decision_reason":  "Action permitted by policy",
    "decision_source":  "backend_evaluation",
}

FLAG_RESP = {
    "policy_decision":  "allow_with_flag",
    "decision_reason":  "Action flagged for review",
    "decision_source":  "backend_evaluation",
}


def deny_resp(action_name: str = "") -> dict:
    return {
        "policy_decision":  "deny",
        "decision_reason":  f"Action {action_name!r} blocked by policy",
        "decision_source":  "backend_evaluation",
        # policy_name in _POLICY_REMEDIATION → remediation + docs_url auto-filled
        "policy_name":      "cumulative_financial_threshold",
        "remediation":      "Request approval via dashboard.",
        "docs_url":         "https://docs.proofrail.ai/policies/thresholds",
    }


# ============================================================
# 5.  Mock backend factory
# ============================================================

def make_mock_post(
    deny_on:         str | None = None,
    flag_on:         str | None = None,
    offline_signal:  bool = False,   # legacy name; now raises BackendUnavailableError
    unavailable:     bool = False,   # raises BackendUnavailableError (Scenario 4)
):
    """
    Return (async_mock_post, calls_list).

    The returned coroutine replaces proofrail.client._post.  Every call is
    appended to calls_list as {"path": str, "body": dict}.

    Failure modes are mutually exclusive and applied to ALL requests so that
    even the chain-start call can be intercepted.
    """
    calls: list[dict] = []

    async def mock_post(path: str, body: dict, action_type: str | None = None) -> dict:
        calls.append({"path": path, "body": dict(body or {})})

        if unavailable:
            raise BackendUnavailableError("backend down", fail_mode="deny")
        if offline_signal:
            raise BackendUnavailableError("backend down", fail_mode="allow")

        if path == "/v1/chains":
            return {"id": CHAIN_ID}
        if path.endswith("/complete"):
            return {"id": CHAIN_ID, "status": "completed"}

        # /v1/chains/{id}/events
        action_name = (body or {}).get("action_name", "")
        if deny_on and action_name == deny_on:
            return deny_resp(action_name)
        if flag_on and action_name == flag_on:
            return FLAG_RESP
        return ALLOW_RESP

    return mock_post, calls


# ============================================================
# 6.  Assertion helpers (module-level, not fixtures)
# ============================================================

def assert_event_recorded(
    calls: list[dict],
    *,
    agent_name:  str | None = None,
    action_name: str | None = None,
    action_type: str | None = None,
) -> None:
    """Assert at least one /events POST matches all supplied criteria."""
    event_calls = [c for c in calls if "events" in c["path"]]
    for c in event_calls:
        b = c["body"]
        if agent_name is not None and b.get("agent_name") != agent_name:
            continue
        if action_name is not None and b.get("action_name") != action_name:
            continue
        if action_type is not None and b.get("action_type") != action_type:
            continue
        return  # match found
    raise AssertionError(
        f"No event found matching agent_name={agent_name!r} "
        f"action_name={action_name!r} action_type={action_type!r}.\n"
        f"Events seen: {[c['body'] for c in event_calls]}"
    )


def assert_chain_completed(calls: list[dict]) -> None:
    """Assert at least one /complete POST was made."""
    hits = [c for c in calls if c["path"].endswith("/complete")]
    assert hits, (
        f"No chain completion call found. Paths: {[c['path'] for c in calls]}"
    )


def count_event_calls(calls: list[dict]) -> int:
    """Count synchronous /events POST calls."""
    return sum(1 for c in calls if "events" in c["path"])


# ============================================================
# 7.  Fixtures
# ============================================================

@pytest.fixture(autouse=True)
def proofrail_dev():
    """
    Default SDK config for all integration tests.

    Fast-path is off and fail-closed transport is the default. Tests that
    intentionally cover deprecated compatibility knobs opt into them locally.
    """
    proofrail.init(
        api_key="prail_test",
        backend_url="http://localhost:9999",
        environment="development",
        enable_local_fast_path=False,
        fail_mode="deny",
    )


@pytest.fixture
def running_event_loop_in_thread():
    """
    Fixture for CrewAI Strategy-B tests: starts a real asyncio event loop in a
    background daemon thread and yields it.  The loop is stopped on teardown.

    CrewAI's monkey-patch calls asyncio.run_coroutine_threadsafe(coro, loop)
    from the *main* thread; the coro must be scheduled on a loop that is
    actually *running* in a separate thread.
    """
    import threading

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
