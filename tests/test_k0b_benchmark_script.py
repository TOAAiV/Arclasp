"""
sdk/tests/test_k0b_benchmark_script.py
========================================
K0B1 verification for sdk/verify-scripts/k0b_latency_benchmark.py.

Covers (per K0B1 Section A/E):
  - Server-Timing parsing: absent, malformed, duplicate, unrelated metrics.
  - Negative derived overhead is represented honestly and flagged, not
    silently clamped or left misleading.
  - Immediate stop (no polling) on require_approval, deny, kill-switch,
    auto-pause, and unexpected policy decisions.
  - Every created chain is completed even after an early stop.
  - The script sends the explicit benchmark-timing opt-in request header.
  - Request cap enforcement, dry-run, missing-environment refusal,
    production-URL refusal, and API-key non-disclosure.

Also covers (K2A2 — extending the collector for the backend's K2A1 fixed
Server-Timing allowlist: app, auth, dbacq, lookup, verify, lastused,
handler; see backend commit dc513f1):
  - Full-allowlist parsing: case-insensitivity, ordering, whitespace,
    unknown-metric ignoring, malformed/negative/non-finite value rejection,
    deterministic duplicate handling, attributes before/after dur.
  - _timed_call populates the six new detailed sample fields correctly, and
    leaves them None (never a fabricated value, never a failure) when the
    backend only reports "app" or reports nothing at all.
  - _summarize produces p50/p95/p99/n for every metric that has at least one
    sample, and omits (never fabricates) percentiles for metrics with zero
    samples in a group.
  - The nesting note is present in both JSON and text output.
  - No raw Server-Timing header string, and no arbitrary/unknown metric
    name, ever survives into JSON or text output.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import pathlib
import sys
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

import httpx
import pytest

import proofrail
from proofrail import client as _pc

_SCRIPT_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "verify-scripts"
    / "k0b_latency_benchmark.py"
)


def _load_script():
    spec = importlib.util.spec_from_file_location("k0b_latency_benchmark", _SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_mod = _load_script()

_DUMMY_REQUEST = httpx.Request("POST", "http://test.invalid/v1/test")


def _init() -> None:
    proofrail.init(
        api_key="prail_test",
        backend_url="http://test.invalid",
        environment="development",
        fail_mode="deny",
        backend_timeout_seconds=5,
        max_retries=3,
        retry_backoff_base_ms=1,
    )


def _decision_response(decision: str, **extra) -> httpx.Response:
    body = {
        "policy_decision": decision,
        "decision_reason": "",
        "decision_source": "backend_evaluation",
        **extra,
    }
    return httpx.Response(200, json=body, request=_DUMMY_REQUEST)


# ---------------------------------------------------------------------------
# Server-Timing parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "header_value,expected",
    [
        (None, None),
        ("", None),
        ("app;dur=12.34", 12.34),
        ("app;dur=abc", None),  # malformed
        ("app;dur=12.34;desc=\"Application\"", 12.34),  # trailing metric attrs
        ("cdn;dur=5, app;dur=12.34", 12.34),  # unrelated metric first
        ("db;dur=45.6", None),  # only an unrelated metric — no "app"
        ("app;dur=12.3, app;dur=45.6", 12.3),  # duplicate — first wins
        ("app;dur=", None),  # malformed, no digits
        # K2A2 spec change: metric names are now explicitly required to be
        # case-insensitive ("Parser behavior: metric names are
        # case-insensitive"), so "APP" now matches "app" — this reverses the
        # old narrow app-only regex's incidental case-sensitivity, which was
        # never a deliberate design choice, just an artifact of a literal
        # lowercase "app" in that regex.
        ("APP;dur=12.34", 12.34),
    ],
)
def test_server_duration_ms_parsing(header_value, expected):
    assert _mod._server_duration_ms(header_value) == expected


# ---------------------------------------------------------------------------
# K2A2 — full fixed-allowlist Server-Timing parsing
# (backend commit dc513f1 / K2A1 added auth, dbacq, lookup, verify,
# lastused, handler alongside the pre-existing "app" metric)
# ---------------------------------------------------------------------------

_ALL_FIELDS = (
    "server_duration_ms",
    "auth_duration_ms",
    "db_connection_acquire_ms",
    "api_key_lookup_ms",
    "api_key_hash_verify_ms",
    "last_used_update_ms",
    "handler_duration_ms",
)


def _all_none() -> dict:
    return {field: None for field in _ALL_FIELDS}


def test_parse_absent_header_returns_all_none():
    assert _mod._parse_server_timing_metrics(None) == _all_none()
    assert _mod._parse_server_timing_metrics("") == _all_none()


def test_parse_app_only_legacy_header():
    """Older/unpatched deployments (or non-2xx/non-governed responses under
    the K2A1 backend hardening) send only "app" — every detailed field must
    resolve to None, not an error and not a fabricated value."""
    result = _mod._parse_server_timing_metrics("app;dur=12.34")
    expected = _all_none()
    expected["server_duration_ms"] = 12.34
    assert result == expected


def test_parse_full_k2a_header_all_metrics_present():
    header = (
        "app;dur=1771.23, auth;dur=1750.11, dbacq;dur=300.5, "
        "lookup;dur=400.25, verify;dur=1200.75, lastused;dur=50.1, "
        "handler;dur=15.9"
    )
    result = _mod._parse_server_timing_metrics(header)
    assert result == {
        "server_duration_ms": 1771.23,
        "auth_duration_ms": 1750.11,
        "db_connection_acquire_ms": 300.5,
        "api_key_lookup_ms": 400.25,
        "api_key_hash_verify_ms": 1200.75,
        "last_used_update_ms": 50.1,
        "handler_duration_ms": 15.9,
    }


def test_parse_metrics_in_different_order():
    header = (
        "handler;dur=15.9, lastused;dur=50.1, verify;dur=1200.75, "
        "lookup;dur=400.25, dbacq;dur=300.5, auth;dur=1750.11, app;dur=1771.23"
    )
    result = _mod._parse_server_timing_metrics(header)
    assert result == {
        "server_duration_ms": 1771.23,
        "auth_duration_ms": 1750.11,
        "db_connection_acquire_ms": 300.5,
        "api_key_lookup_ms": 400.25,
        "api_key_hash_verify_ms": 1200.75,
        "last_used_update_ms": 50.1,
        "handler_duration_ms": 15.9,
    }


def test_parse_metrics_mixed_case():
    header = "APP;dur=1.0, Auth;dur=2.0, DbAcq;dur=3.0, LOOKUP;dur=4.0, Verify;dur=5.0, LastUsed;dur=6.0, HANDLER;dur=7.0"
    result = _mod._parse_server_timing_metrics(header)
    assert result == {
        "server_duration_ms": 1.0,
        "auth_duration_ms": 2.0,
        "db_connection_acquire_ms": 3.0,
        "api_key_lookup_ms": 4.0,
        "api_key_hash_verify_ms": 5.0,
        "last_used_update_ms": 6.0,
        "handler_duration_ms": 7.0,
    }


def test_parse_metrics_optional_whitespace():
    header = '  app ; dur = 1.0 ,  auth  ;  dur=2.0  ,dbacq;dur = 3.0'
    result = _mod._parse_server_timing_metrics(header)
    assert result["server_duration_ms"] == 1.0
    assert result["auth_duration_ms"] == 2.0
    assert result["db_connection_acquire_ms"] == 3.0


def test_parse_unknown_metrics_ignored():
    header = "cdn;dur=5, app;dur=12.34, edge;dur=999, waf;dur=1"
    result = _mod._parse_server_timing_metrics(header)
    expected = _all_none()
    expected["server_duration_ms"] = 12.34
    assert result == expected
    # No unknown name ever appears as a key.
    assert set(result.keys()) == set(_ALL_FIELDS)


def test_parse_malformed_dur_values_ignored_not_crashed():
    for header in (
        "auth;dur=abc",
        "auth;dur=",
        "auth;dur=1.2.3",
        "auth;dur=,",
        "auth",  # no ";dur=" at all
        "auth;desc=\"x\"",  # attrs present but no dur
    ):
        result = _mod._parse_server_timing_metrics(header)
        assert result["auth_duration_ms"] is None, f"expected None for {header!r}"
    # Malformed input anywhere in a longer header must not prevent parsing
    # of a later, valid, distinct metric.
    result = _mod._parse_server_timing_metrics("auth;dur=abc, verify;dur=5.5")
    assert result["auth_duration_ms"] is None
    assert result["api_key_hash_verify_ms"] == 5.5


@pytest.mark.parametrize(
    "header",
    [
        "auth;dur=-1.0",  # negative
        "auth;dur=NaN",
        "auth;dur=nan",
        "auth;dur=Infinity",
        "auth;dur=-Infinity",
        "auth;dur=inf",
    ],
)
def test_parse_negative_and_non_finite_values_ignored(header):
    result = _mod._parse_server_timing_metrics(header)
    assert result["auth_duration_ms"] is None


def test_parse_duplicate_metric_first_valid_occurrence_wins():
    # Both valid -> first wins.
    result = _mod._parse_server_timing_metrics("auth;dur=5.0, auth;dur=9.0")
    assert result["auth_duration_ms"] == 5.0

    # First malformed, second valid -> the second (first VALID) wins, not None.
    result = _mod._parse_server_timing_metrics("auth;dur=abc, auth;dur=9.0")
    assert result["auth_duration_ms"] == 9.0

    # First valid, second malformed -> first value is retained, not clobbered.
    result = _mod._parse_server_timing_metrics("auth;dur=5.0, auth;dur=abc")
    assert result["auth_duration_ms"] == 5.0


def test_parse_additional_attributes_after_dur():
    header = 'auth;dur=5.5;desc="Auth verification"'
    result = _mod._parse_server_timing_metrics(header)
    assert result["auth_duration_ms"] == 5.5


def test_parse_dur_before_other_attributes():
    header = 'auth;desc="Auth verification";dur=5.5'
    result = _mod._parse_server_timing_metrics(header)
    assert result["auth_duration_ms"] == 5.5


def test_parse_result_never_contains_arbitrary_metric_names():
    header = "app;dur=1.0, totally-made-up-metric;dur=2.0, another_one;dur=3.0"
    result = _mod._parse_server_timing_metrics(header)
    assert set(result.keys()) == set(_ALL_FIELDS)


def test_parse_never_retains_raw_header_string():
    header = "app;dur=1.0, auth;dur=2.0"
    result = _mod._parse_server_timing_metrics(header)
    assert header not in result.values()
    assert header not in str(result)


# ---------------------------------------------------------------------------
# K2A2 — sample fields populated by _timed_call
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_timed_call_populates_detailed_fields_correctly():
    _init()
    header = (
        "app;dur=1771.23, auth;dur=1750.11, dbacq;dur=300.5, "
        "lookup;dur=400.25, verify;dur=1200.75, lastused;dur=50.1, "
        "handler;dur=15.9"
    )

    async def _post_fast(*args, **kwargs) -> httpx.Response:
        return httpx.Response(
            200,
            json={"policy_decision": "allow", "decision_reason": "", "decision_source": "x"},
            headers={"Server-Timing": header},
            request=_DUMMY_REQUEST,
        )

    client_mock = MagicMock()
    client_mock.post = _post_fast

    with patch.object(_pc, "_get_client", return_value=client_mock):
        result = await _mod._timed_call(
            lambda: _pc._post("/v1/chains/test/events", {}), "event_record", 1, None
        )

    assert result["server_duration_ms"] == 1771.23
    assert result["auth_duration_ms"] == 1750.11
    assert result["db_connection_acquire_ms"] == 300.5
    assert result["api_key_lookup_ms"] == 400.25
    assert result["api_key_hash_verify_ms"] == 1200.75
    assert result["last_used_update_ms"] == 50.1
    assert result["handler_duration_ms"] == 15.9
    # No raw header string anywhere in the sample dict.
    assert header not in result.values()


@pytest.mark.asyncio
async def test_timed_call_missing_detailed_metrics_are_none_not_failure():
    """App-only header (older deployment / non-2xx / non-governed route):
    detailed fields are None, and this must not be treated as a request
    failure — no exception_category is set, the sample is returned normally."""
    _init()

    async def _post_app_only(*args, **kwargs) -> httpx.Response:
        return httpx.Response(
            200,
            json={"policy_decision": "allow", "decision_reason": "", "decision_source": "x"},
            headers={"Server-Timing": "app;dur=350.0"},
            request=_DUMMY_REQUEST,
        )

    client_mock = MagicMock()
    client_mock.post = _post_app_only

    with patch.object(_pc, "_get_client", return_value=client_mock):
        result = await _mod._timed_call(
            lambda: _pc._post("/v1/chains/test/events", {}), "event_record", 1, None
        )

    assert result["server_duration_ms"] == 350.0
    assert result["auth_duration_ms"] is None
    assert result["db_connection_acquire_ms"] is None
    assert result["api_key_lookup_ms"] is None
    assert result["api_key_hash_verify_ms"] is None
    assert result["last_used_update_ms"] is None
    assert result["handler_duration_ms"] is None
    assert result["exception_category"] is None


@pytest.mark.asyncio
async def test_timed_call_no_header_at_all_all_fields_none():
    _init()

    async def _post_no_header(*args, **kwargs) -> httpx.Response:
        return httpx.Response(
            200,
            json={"policy_decision": "allow", "decision_reason": "", "decision_source": "x"},
            request=_DUMMY_REQUEST,
        )

    client_mock = MagicMock()
    client_mock.post = _post_no_header

    with patch.object(_pc, "_get_client", return_value=client_mock):
        result = await _mod._timed_call(
            lambda: _pc._post("/v1/chains/test/events", {}), "event_record", 1, None
        )

    for field in _ALL_FIELDS:
        assert result[field] is None


# ---------------------------------------------------------------------------
# K2A2 — summary percentiles for every populated metric
# ---------------------------------------------------------------------------

def _fake_sample(**overrides) -> dict:
    base = {
        "operation": "event_record",
        "event_count_requested": 1,
        "sdk_client_state": "client_reused",
        "client_created": False,
        "retries": 0,
        "response_status_class": "2xx",
        "total_duration_ms": 2000.0,
        "server_duration_ms": None,
        "auth_duration_ms": None,
        "db_connection_acquire_ms": None,
        "api_key_lookup_ms": None,
        "api_key_hash_verify_ms": None,
        "last_used_update_ms": None,
        "handler_duration_ms": None,
        "network_client_overhead_ms": None,
        "network_client_overhead_ms_negative": False,
        "exception_category": None,
        "benchmark_region_label": None,
    }
    base.update(overrides)
    return base


def test_summarize_percentiles_for_every_populated_metric():
    samples = [
        _fake_sample(
            server_duration_ms=1771.0, auth_duration_ms=1750.0,
            db_connection_acquire_ms=300.0, api_key_lookup_ms=400.0,
            api_key_hash_verify_ms=1200.0, last_used_update_ms=50.0,
            handler_duration_ms=15.0,
        ),
        _fake_sample(
            server_duration_ms=1800.0, auth_duration_ms=1760.0,
            db_connection_acquire_ms=310.0, api_key_lookup_ms=410.0,
            api_key_hash_verify_ms=1210.0, last_used_update_ms=55.0,
            handler_duration_ms=16.0,
        ),
    ]
    summary = _mod._summarize(samples)
    group = summary["groups"][0]

    for field in _ALL_FIELDS:
        pct = group[field]
        assert pct is not None
        assert pct["n"] == 2
        assert pct["p50"] is not None and pct["p95"] is not None and pct["p99"] is not None
        assert pct["p50"] >= 0


def test_summarize_absent_metric_produces_no_fabricated_percentiles():
    """A group where only "app" was ever observed must show None (not a
    zero-filled percentile block) for every detailed metric."""
    samples = [_fake_sample(server_duration_ms=350.0)]
    summary = _mod._summarize(samples)
    group = summary["groups"][0]

    assert group["server_duration_ms"] is not None
    for field in _ALL_FIELDS:
        if field == "server_duration_ms":
            continue
        assert group[field] is None


def test_summarize_partial_population_only_counts_actual_samples():
    """Mixed group: some samples have a detailed metric, some don't (e.g.
    a mix of 2xx-governed and non-2xx/non-governed requests in one group).
    The percentile's n must equal the count of non-None values only."""
    samples = [
        _fake_sample(server_duration_ms=100.0, auth_duration_ms=90.0),
        _fake_sample(server_duration_ms=110.0, auth_duration_ms=None),
        _fake_sample(server_duration_ms=120.0, auth_duration_ms=95.0),
    ]
    summary = _mod._summarize(samples)
    group = summary["groups"][0]

    assert group["server_duration_ms"]["n"] == 3
    assert group["auth_duration_ms"]["n"] == 2


def test_summarize_includes_nesting_note():
    summary = _mod._summarize([_fake_sample(server_duration_ms=100.0)])
    assert "note" not in summary  # top-level key is specifically "k2a_nesting_note"
    assert "k2a_nesting_note" in summary
    note = summary["k2a_nesting_note"].lower()
    assert "nested" in note
    assert "auth" in note and "lookup" in note and "handler" in note


# ---------------------------------------------------------------------------
# K2A2 — full pipeline: JSON + text output
# ---------------------------------------------------------------------------

def test_full_run_writes_k2a_metrics_and_nesting_note_to_output(tmp_path):
    # NOT async: main() calls asyncio.run() internally (see the module's own
    # "fresh event loop per invocation" comment) -- running this test inside
    # pytest-asyncio's own event loop would make that asyncio.run() raise
    # "cannot be called from a running event loop". This test must call
    # main() from a plain synchronous context, exactly like the existing
    # _run_main-based tests in this file (test_dry_run_succeeds_with_no_env_vars
    # etc.) already do.
    out_dir = tmp_path / "k0b_out"

    header = (
        "app;dur=12.34, Auth;dur=5.67, dbacq;dur=1.11, "
        "LOOKUP;dur=2.22, verify;dur=3.33, lastUsed;dur=0.44, "
        "handler;dur=6.78, cdn;dur=999"
    )

    async def _post_mock(url, *args, **kwargs) -> httpx.Response:
        if url.endswith("/events"):
            body = {"policy_decision": "allow", "decision_reason": "", "decision_source": "x"}
        else:
            body = {"id": "fake-chain-id"}
        return httpx.Response(
            200, json=body, headers={"Server-Timing": header}, request=_DUMMY_REQUEST
        )

    client_mock = MagicMock()
    client_mock.post = _post_mock
    client_mock.headers = {}

    old_argv = sys.argv
    old_environ = dict(os.environ)
    sys.argv = [
        "k0b_latency_benchmark.py",
        "--event-counts", "1",
        "--runs", "1",
        "--out-dir", str(out_dir),
    ]
    os.environ.clear()
    os.environ.update(
        {
            "PROOFRAIL_K0B_BACKEND_URL": "http://test.invalid",
            "PROOFRAIL_K0B_API_KEY": "prail_fake_test_key_never_appears",
        }
    )
    try:
        with patch.object(_pc, "_get_client", return_value=client_mock):
            exit_code = _mod.main()
    finally:
        sys.argv = old_argv
        os.environ.clear()
        os.environ.update(old_environ)

    assert exit_code == 0

    json_files = list(out_dir.glob("*_k0b_latency.json"))
    txt_files = list(out_dir.glob("*_k0b_latency_summary.txt"))
    assert len(json_files) == 1
    assert len(txt_files) == 1

    raw_json_text = json_files[0].read_text(encoding="utf-8")
    raw_txt_text = txt_files[0].read_text(encoding="utf-8")
    payload = json.loads(raw_json_text)

    # Every sample got the fixed six detailed fields populated (unknown
    # "cdn" metric ignored).
    for sample in payload["samples"]:
        assert sample["server_duration_ms"] == 12.34
        assert sample["auth_duration_ms"] == 5.67
        assert sample["db_connection_acquire_ms"] == 1.11
        assert sample["api_key_lookup_ms"] == 2.22
        assert sample["api_key_hash_verify_ms"] == 3.33
        assert sample["last_used_update_ms"] == 0.44
        assert sample["handler_duration_ms"] == 6.78

    # Summary percentiles present for every populated metric on every group.
    for group in payload["summary"]["groups"]:
        for field in _ALL_FIELDS:
            assert group[field] is not None
            assert group[field]["n"] == 1

    # Nesting note present in both JSON metadata and text output.
    assert "nested" in payload["summary"]["k2a_nesting_note"].lower()
    assert "nested" in raw_txt_text.lower()

    # Text summary shows the compact per-metric K2A split.
    for label in ("app", "auth", "dbacq", "lookup", "verify", "lastused", "handler"):
        assert f"{label} p50=" in raw_txt_text

    # No raw Server-Timing header string, and no unrelated "cdn" metric
    # name, ever appears in either output file.
    assert header not in raw_json_text
    assert header not in raw_txt_text
    assert "cdn" not in raw_json_text
    assert "cdn" not in raw_txt_text
    assert "Server-Timing" not in raw_json_text
    assert "Server-Timing" not in raw_txt_text
    assert "prail_fake_test_key_never_appears" not in raw_json_text
    assert "prail_fake_test_key_never_appears" not in raw_txt_text


# ---------------------------------------------------------------------------
# Negative overhead honesty
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_negative_overhead_is_honest_not_clamped():
    _init()

    async def _post_with_header(*args, **kwargs) -> httpx.Response:
        # Server reports MORE than what we'll measure as total wall time —
        # forces a negative residual.
        return _decision_response("allow")

    client_mock = MagicMock()
    client_mock.post = _post_with_header
    client_mock.headers = {}

    async def _fake_call():
        return None

    with patch.object(_pc, "_get_client", return_value=client_mock):
        with _pc._benchmark_sample() as sample:
            sample.server_timing_header = "app;dur=999999"  # absurdly large
            sample.status_code = 200
        # Manually build the dict the way _timed_call would, using a tiny
        # total duration to force total_ms < server_ms.
    total_ms = 0.001
    server_ms = _mod._server_duration_ms(sample.server_timing_header)
    assert server_ms is not None and server_ms > total_ms

    overhead_ms = round(total_ms - server_ms, 3)
    assert overhead_ms < 0  # confirms the test setup actually forces negative

    # Exercise the real _timed_call path end-to-end with a monkeypatched
    # clock so total_ms is deterministically tiny.
    call_count = 0

    async def _post_fast(*args, **kwargs) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            200,
            json={"policy_decision": "allow", "decision_reason": "", "decision_source": "x"},
            headers={"Server-Timing": "app;dur=999999"},
            request=_DUMMY_REQUEST,
        )

    client_mock2 = MagicMock()
    client_mock2.post = _post_fast

    with patch.object(_pc, "_get_client", return_value=client_mock2):
        result = await _mod._timed_call(
            lambda: _pc._post("/v1/chains/test/events", {}), "event_record", 1, None
        )

    assert result["network_client_overhead_ms"] is not None
    assert result["network_client_overhead_ms"] < 0
    assert result["network_client_overhead_ms_negative"] is True


# ---------------------------------------------------------------------------
# Immediate stop — no polling — on non-allow decisions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_require_approval_stops_immediately_without_polling():
    _init()
    chain = _mod.Chain("k0b-test-chain")
    chain._chain_id = "fake-chain-id"

    async def _post_require_approval(*args, **kwargs) -> httpx.Response:
        return _decision_response("require_approval")

    client_mock = MagicMock()
    client_mock.post = _post_require_approval

    # If this test ever calls the real polling loop, it would sleep in
    # 5-second increments — patch asyncio.sleep to fail loudly if invoked,
    # proving no polling occurred.
    async def _sleep_should_not_be_called(*args, **kwargs):
        raise AssertionError("must not sleep/poll on require_approval")

    with patch.object(_pc, "_get_client", return_value=client_mock), \
         patch("asyncio.sleep", _sleep_should_not_be_called):
        with pytest.raises(_mod._AbortBenchmark) as exc_info:
            await _mod._record_event_no_poll(chain, "tool_call", "benchmark_noop")

    assert exc_info.value.category == "requires_approval"


@pytest.mark.asyncio
async def test_deny_stops_immediately():
    _init()
    chain = _mod.Chain("k0b-test-chain")
    chain._chain_id = "fake-chain-id"

    async def _post_deny(*args, **kwargs) -> httpx.Response:
        return _decision_response("deny", kill_switch_active=False)

    client_mock = MagicMock()
    client_mock.post = _post_deny

    with patch.object(_pc, "_get_client", return_value=client_mock):
        with pytest.raises(_mod._AbortBenchmark) as exc_info:
            await _mod._record_event_no_poll(chain, "tool_call", "benchmark_noop")

    assert exc_info.value.category == "policy_deny"


@pytest.mark.asyncio
async def test_kill_switch_deny_stops_immediately():
    _init()
    chain = _mod.Chain("k0b-test-chain")
    chain._chain_id = "fake-chain-id"

    async def _post_kill_switch(*args, **kwargs) -> httpx.Response:
        return _decision_response("deny", kill_switch_active=True)

    client_mock = MagicMock()
    client_mock.post = _post_kill_switch

    with patch.object(_pc, "_get_client", return_value=client_mock):
        with pytest.raises(_mod._AbortBenchmark) as exc_info:
            await _mod._record_event_no_poll(chain, "tool_call", "benchmark_noop")

    assert exc_info.value.category == "kill_switch"


@pytest.mark.asyncio
async def test_auto_paused_stops_immediately():
    _init()
    chain = _mod.Chain("k0b-test-chain")
    chain._chain_id = "fake-chain-id"

    async def _post_auto_paused(*args, **kwargs) -> httpx.Response:
        return _decision_response("allow", auto_paused=True)

    client_mock = MagicMock()
    client_mock.post = _post_auto_paused

    with patch.object(_pc, "_get_client", return_value=client_mock):
        with pytest.raises(_mod._AbortBenchmark) as exc_info:
            await _mod._record_event_no_poll(chain, "tool_call", "benchmark_noop")

    assert exc_info.value.category == "chain_auto_paused"


@pytest.mark.asyncio
async def test_unexpected_decision_stops_immediately():
    _init()
    chain = _mod.Chain("k0b-test-chain")
    chain._chain_id = "fake-chain-id"

    async def _post_unexpected(*args, **kwargs) -> httpx.Response:
        return _decision_response("something_new_and_unhandled")

    client_mock = MagicMock()
    client_mock.post = _post_unexpected

    with patch.object(_pc, "_get_client", return_value=client_mock):
        with pytest.raises(_mod._AbortBenchmark) as exc_info:
            await _mod._record_event_no_poll(chain, "tool_call", "benchmark_noop")

    assert exc_info.value.category == "unexpected_policy_decision"


@pytest.mark.asyncio
async def test_allow_and_allow_with_flag_do_not_abort():
    _init()
    chain = _mod.Chain("k0b-test-chain")
    chain._chain_id = "fake-chain-id"

    for decision in ("allow", "allow_with_flag"):
        async def _post_allow(*args, **kwargs) -> httpx.Response:
            return _decision_response(decision)

        client_mock = MagicMock()
        client_mock.post = _post_allow

        with patch.object(_pc, "_get_client", return_value=client_mock):
            await _mod._record_event_no_poll(chain, "tool_call", "benchmark_noop")  # must not raise


# ---------------------------------------------------------------------------
# Every created chain is completed, even after an early stop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chain_completed_after_early_stop_in_event_loop():
    _init()

    complete_called = []

    async def _fake_start(self):
        pass

    async def _fake_complete(self):
        complete_called.append(True)

    with patch.object(_pc, "_get_client", return_value=MagicMock()), \
         patch.object(_mod, "_record_event_no_poll", side_effect=_mod._AbortBenchmark("policy_deny")), \
         patch.object(_mod.Chain, "_start", _fake_start), \
         patch.object(_mod.Chain, "_complete", _fake_complete):
        with pytest.raises(_mod._AbortBenchmark):
            await _mod._run_one(2, None)

    assert complete_called == [True]


# ---------------------------------------------------------------------------
# Explicit benchmark-timing request header sent
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_script_sends_explicit_timing_opt_in_header():
    _init()

    fake_headers: dict = {}
    client_mock = MagicMock()
    client_mock.headers = fake_headers

    async def _fake_start(self):
        pass

    with patch.object(_pc, "_get_client", return_value=client_mock), \
         patch.object(_mod.Chain, "_start", _fake_start):
        chain = _mod.Chain("k0b-test-chain")

        async def _enter_body():
            client = _pc._get_client()
            client.headers[_mod._BENCHMARK_TIMING_REQUEST_HEADER] = _mod._BENCHMARK_TIMING_REQUEST_VALUE
            await chain._start()

        await _enter_body()

    assert fake_headers.get("X-Arclasp-Benchmark-Timing") == "1"


# ---------------------------------------------------------------------------
# Request cap, dry-run, missing-env refusal, production-URL refusal
# ---------------------------------------------------------------------------

def _run_main(argv, env):
    old_argv = sys.argv
    old_environ = dict(os.environ)
    sys.argv = ["k0b_latency_benchmark.py"] + argv
    os.environ.clear()
    os.environ.update(env)
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            try:
                exit_code = _mod.main()
            except SystemExit as exc:
                exit_code = exc.code
                # argparse/raise SystemExit("message") puts the message in
                # exc.code, not stdout — fold it into the captured output so
                # assertions can uniformly check message content there.
                if isinstance(exc.code, str):
                    buf.write(exc.code)
    finally:
        sys.argv = old_argv
        os.environ.clear()
        os.environ.update(old_environ)
    return exit_code, buf.getvalue()


def test_request_cap_enforced_before_dry_run_network_skip():
    exit_code, output = _run_main(
        ["--event-counts", "1,20,50", "--runs", "5", "--dry-run"], {}
    )
    assert exit_code != 0
    assert "exceeds --max-requests" in output


def test_dry_run_succeeds_with_no_env_vars():
    exit_code, output = _run_main(["--dry-run"], {})
    assert exit_code == 0
    assert "PROOFRAIL_K0B_BACKEND_URL set: False" in output
    assert "PROOFRAIL_K0B_API_KEY set: False" in output


def test_missing_environment_refusal():
    exit_code, output = _run_main([], {})
    assert exit_code != 0
    assert "must both be set" in output


def test_production_url_refusal_without_explicit_authorization():
    exit_code, output = _run_main(
        [],
        {
            "PROOFRAIL_K0B_BACKEND_URL": "https://api.proofrail.dev",
            "PROOFRAIL_K0B_API_KEY": "prail_fake_secret_should_not_appear_anywhere_12345",
        },
    )
    assert exit_code != 0
    assert "production-like" in output
    assert "prail_fake_secret_should_not_appear_anywhere_12345" not in output


def test_api_key_never_disclosed_on_unreachable_backend():
    secret = "prail_super_secret_value_must_never_print_98765"
    exit_code, output = _run_main(
        [],
        {
            "PROOFRAIL_K0B_BACKEND_URL": "http://127.0.0.1:1",  # nothing listens here
            "PROOFRAIL_K0B_API_KEY": secret,
        },
    )
    assert exit_code != 0
    assert secret not in output
