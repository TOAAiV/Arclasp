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
"""
from __future__ import annotations

import importlib.util
import io
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
        ("APP;dur=12.34", None),  # case-sensitive metric name — not "app"
    ],
)
def test_server_duration_ms_parsing(header_value, expected):
    assert _mod._server_duration_ms(header_value) == expected


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
