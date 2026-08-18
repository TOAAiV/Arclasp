"""
sdk/tests/test_k0b_benchmark_hook.py
=====================================
K0B internal opt-in benchmark instrumentation (arclasp.client._benchmark_sample).

Covers:
  1. Retry count is recorded inside an active benchmark sample.
  2. Client created/reused classification is recorded correctly across
     successive _get_client() calls on the same event loop.
  3. Server-Timing response header and status code are captured when present.
  4. Exception category is recorded on exhausted-retry network failures.
  5. Outside an active `_benchmark_sample()` context, no sample is populated
     and ordinary calls are unaffected (default/no-op behavior).
  6. Retry counts reset per request (independent sequential samples).
  7. Concurrent asyncio tasks do not leak retry counts into each other.
  8. Nested benchmark contexts restore the outer sample on exit and do not
     leak recordings between inner/outer.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import httpx
import pytest

import arclasp
from arclasp import client as _pc
from arclasp.exceptions import BackendUnavailableError

_DUMMY_REQUEST = httpx.Request("POST", "http://test.invalid/v1/test")


def _allow_response(headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "policy_decision": "allow",
            "decision_reason": "",
            "decision_source": "backend_evaluation",
        },
        headers=headers or {},
        request=_DUMMY_REQUEST,
    )


def _init_deny() -> None:
    arclasp.init(
        api_key="prail_test",
        backend_url="http://test.invalid",
        environment="development",
        fail_mode="deny",
        backend_timeout_seconds=5,
        max_retries=3,
        retry_backoff_base_ms=1,
    )


# ---------------------------------------------------------------------------
# Test 1 — retry count recorded
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_benchmark_sample_records_retry_count():
    _init_deny()

    call_count = 0

    async def _flaky_post(*args, **kwargs) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise httpx.ConnectError("connection refused")
        return _allow_response()

    client_mock = MagicMock()
    client_mock.post = _flaky_post

    with patch.object(_pc, "_get_client", return_value=client_mock):
        with _pc._benchmark_sample() as sample:
            result = await _pc._post("/v1/chains/test-id/events", {"action_type": "tool_call"})

    assert result["policy_decision"] == "allow"
    assert sample.retries == 2


# ---------------------------------------------------------------------------
# Test 2 — client created/reused classification
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_benchmark_sample_records_client_created_then_reused():
    _init_deny()  # clears _clients_by_loop for the current (running) loop

    with _pc._benchmark_sample() as sample_first:
        client_first = _pc._get_client()
    assert sample_first.client_created is True

    with _pc._benchmark_sample() as sample_second:
        client_second = _pc._get_client()
    assert sample_second.client_created is False
    assert client_second is client_first


# ---------------------------------------------------------------------------
# Test 3 — Server-Timing header and status code captured
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_benchmark_sample_records_server_timing_and_status():
    _init_deny()

    async def _post_with_header(*args, **kwargs) -> httpx.Response:
        return _allow_response(headers={"Server-Timing": "app;dur=12.34"})

    client_mock = MagicMock()
    client_mock.post = _post_with_header

    with patch.object(_pc, "_get_client", return_value=client_mock):
        with _pc._benchmark_sample() as sample:
            await _pc._post("/v1/chains/test-id/events", {"action_type": "tool_call"})

    assert sample.status_code == 200
    assert sample.server_timing_header == "app;dur=12.34"


# ---------------------------------------------------------------------------
# Test 4 — exception category recorded on exhausted retries
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_benchmark_sample_records_exception_category():
    _init_deny()

    async def _always_fails(*args, **kwargs) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client_mock = MagicMock()
    client_mock.post = _always_fails

    with patch.object(_pc, "_get_client", return_value=client_mock):
        with _pc._benchmark_sample() as sample:
            with pytest.raises(BackendUnavailableError):
                await _pc._post("/v1/chains/test-id/events", {"action_type": "tool_call"})

    assert sample.exception_category == "ConnectError"
    assert sample.retries == 3  # max_retries=3 in _init_deny()


# ---------------------------------------------------------------------------
# Test 5 — no-op outside an active benchmark context
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_benchmark_sample_outside_context_is_noop():
    _init_deny()

    async def _post_ok(*args, **kwargs) -> httpx.Response:
        return _allow_response()

    client_mock = MagicMock()
    client_mock.post = _post_ok

    assert _pc._benchmark_ctx.get() is None
    with patch.object(_pc, "_get_client", return_value=client_mock):
        result = await _pc._post("/v1/chains/test-id/events", {"action_type": "tool_call"})

    assert result["policy_decision"] == "allow"
    assert _pc._benchmark_ctx.get() is None


# ---------------------------------------------------------------------------
# Test 6 — retry counts reset per request (independent sequential samples)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retry_count_resets_between_sequential_samples():
    _init_deny()

    call_count = 0

    async def _flaky_twice_then_once(*args, **kwargs) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        # First request retries twice (calls 1,2 fail; call 3 succeeds).
        # Second request retries once (call 4 fails; call 5 succeeds).
        if call_count in (1, 2, 4):
            raise httpx.ConnectError("connection refused")
        return _allow_response()

    client_mock = MagicMock()
    client_mock.post = _flaky_twice_then_once

    with patch.object(_pc, "_get_client", return_value=client_mock):
        with _pc._benchmark_sample() as sample_one:
            await _pc._post("/v1/chains/test-id/events", {"action_type": "tool_call"})
        with _pc._benchmark_sample() as sample_two:
            await _pc._post("/v1/chains/test-id/events", {"action_type": "tool_call"})

    assert sample_one.retries == 2
    assert sample_two.retries == 1  # not 3 — must not accumulate across samples


# ---------------------------------------------------------------------------
# Test 7 — concurrent asyncio task isolation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_tasks_do_not_leak_samples():
    _init_deny()

    async def _task(retry_count: int, delay_s: float) -> int:
        call_count = 0

        async def _flaky(*args, **kwargs) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(delay_s)  # interleave with the other task
            if call_count <= retry_count:
                raise httpx.ConnectError("connection refused")
            return _allow_response()

        client_mock = MagicMock()
        client_mock.post = _flaky

        with patch.object(_pc, "_get_client", return_value=client_mock):
            with _pc._benchmark_sample() as sample:
                await _pc._post("/v1/chains/test-id/events", {"action_type": "tool_call"})
        return sample.retries

    # Task A retries 3 times, Task B retries 1 time — run concurrently so
    # their awaits interleave. Each must observe only its own retry count.
    results = await asyncio.gather(
        _task(retry_count=3, delay_s=0.01),
        _task(retry_count=1, delay_s=0.005),
    )

    assert results == [3, 1]


# ---------------------------------------------------------------------------
# Test 8 — nested benchmark contexts do not leak into each other
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_nested_benchmark_contexts_do_not_leak():
    _init_deny()

    call_count = 0

    async def _flaky_inner_only(*args, **kwargs) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.ConnectError("connection refused")
        return _allow_response()

    client_mock = MagicMock()
    client_mock.post = _flaky_inner_only

    with patch.object(_pc, "_get_client", return_value=client_mock):
        with _pc._benchmark_sample() as outer_sample:
            assert _pc._benchmark_ctx.get() is outer_sample
            with _pc._benchmark_sample() as inner_sample:
                assert _pc._benchmark_ctx.get() is inner_sample
                assert inner_sample is not outer_sample
                await _pc._post("/v1/chains/test-id/events", {"action_type": "tool_call"})
            # After the inner context exits, the ambient sample must be
            # restored to the outer one — not None, not the inner sample.
            assert _pc._benchmark_ctx.get() is outer_sample

    # The inner request's retry was recorded only on the inner sample.
    assert inner_sample.retries == 1
    assert outer_sample.retries == 0
