"""
sdk/tests/test_network_error_handling.py
=========================================
Regression tests for BUG-LR-02 — _retry_with_backoff catches
httpx.NetworkError (ReadError, WriteError) in addition to ConnectError.

Five surfaces:
  1. ReadError is retried and recovers on second attempt.
  2. WriteError is retried and recovers on second attempt.
  3. ConnectError still works — no regression from the change.
  4. RemoteProtocolError does NOT retry — confirms narrow NetworkError
     catch, not the broader TransportError parent.
  5. Exhausted retries on ReadError calls _handle_backend_failure so
     BackendUnavailableError fires even when fail_mode="allow".
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

import arclasp
from arclasp import client as _pc
from arclasp.exceptions import BackendUnavailableError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# httpx.Response requires a request object before raise_for_status() can be
# called.  These tests patch _get_client and route through _post's full path
# (including raise_for_status), so we attach a dummy request.
_DUMMY_REQUEST = httpx.Request("POST", "http://test.invalid/v1/test")


def _chain_create_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={"id": "test-chain-id", "status": "active"},
        request=_DUMMY_REQUEST,
    )


def _allow_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "policy_decision": "allow",
            "decision_reason": "",
            "decision_source": "backend_evaluation",
        },
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
        retry_backoff_base_ms=1,   # 1 ms — tests run fast
    )


def _init_allow() -> None:
    with pytest.warns(DeprecationWarning, match="fail_mode"):
        arclasp.init(
            api_key="prail_test",
            backend_url="http://test.invalid",
            environment="development",
            fail_mode="allow",
            backend_timeout_seconds=5,
            max_retries=3,
            retry_backoff_base_ms=1,
        )


# ---------------------------------------------------------------------------
# Test 1 — ReadError is retried; succeeds on second attempt
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retry_catches_read_error():
    """
    ReadError on attempt 0, success on attempt 1.
    Verify: mock called twice; final result is the successful response.
    """
    _init_deny()

    call_count = 0

    async def _flaky_post(*args, **kwargs) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.ReadError("connection reset by peer")
        return _allow_response()

    client_mock = MagicMock()
    client_mock.post = _flaky_post

    with patch.object(_pc, "_get_client", return_value=client_mock):
        result = await _pc._post("/v1/chains/test-id/events", {"action_type": "tool_call"})

    assert call_count == 2, f"Expected 2 attempts (1 retry), got {call_count}"
    assert result["policy_decision"] == "allow"


# ---------------------------------------------------------------------------
# Test 2 — WriteError is retried; succeeds on second attempt
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retry_catches_write_error():
    """
    WriteError on attempt 0, success on attempt 1.
    Verify: mock called twice; final result is the successful response.
    """
    _init_deny()

    call_count = 0

    async def _flaky_post(*args, **kwargs) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.WriteError("broken pipe")
        return _allow_response()

    client_mock = MagicMock()
    client_mock.post = _flaky_post

    with patch.object(_pc, "_get_client", return_value=client_mock):
        result = await _pc._post("/v1/chains/test-id/events", {"action_type": "tool_call"})

    assert call_count == 2, f"Expected 2 attempts (1 retry), got {call_count}"
    assert result["policy_decision"] == "allow"


# ---------------------------------------------------------------------------
# Test 3 — ConnectError still retried (regression guard)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retry_connect_error_regression():
    """
    ConnectError was caught before the change and must still be caught after
    (ConnectError is a NetworkError subclass).
    """
    _init_deny()

    call_count = 0

    async def _flaky_post(*args, **kwargs) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.ConnectError("connection refused")
        return _allow_response()

    client_mock = MagicMock()
    client_mock.post = _flaky_post

    with patch.object(_pc, "_get_client", return_value=client_mock):
        result = await _pc._post("/v1/chains/test-id/events", {"action_type": "tool_call"})

    assert call_count == 2, f"Expected 2 attempts (1 retry), got {call_count}"
    assert result["policy_decision"] == "allow"


# ---------------------------------------------------------------------------
# Test 4 — RemoteProtocolError does NOT retry (narrow NetworkError catch)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_remote_protocol_error_does_not_retry():
    """
    RemoteProtocolError is a TransportError but NOT a NetworkError subclass —
    it indicates backend health issues and should NOT be silently retried.
    It should propagate immediately as a raw httpx exception.
    """
    _init_deny()

    call_count = 0

    async def _bad_protocol_post(*args, **kwargs) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        raise httpx.RemoteProtocolError("server disconnected without response")

    client_mock = MagicMock()
    client_mock.post = _bad_protocol_post

    with patch.object(_pc, "_get_client", return_value=client_mock):
        with pytest.raises(httpx.RemoteProtocolError):
            await _pc._post("/v1/chains/test-id/events", {"action_type": "tool_call"})

    # Called exactly once — no retry attempted
    assert call_count == 1, (
        f"RemoteProtocolError must NOT be retried, but mock was called {call_count} time(s). "
        "Check that the catch uses NetworkError (not the broader TransportError)."
    )


# ---------------------------------------------------------------------------
# Test 5 — Exhausted retries on ReadError routes through _handle_backend_failure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_exhausted_read_error_retries_fail_closed_on_allow():
    """
    When all retries are exhausted on ReadError and fail_mode='allow', _post
    raises BackendUnavailableError. The allow setting is deprecated and must
    not create an offline allow path.
    """
    _init_allow()

    call_count = 0

    async def _always_fails(*args, **kwargs) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        raise httpx.ReadError("connection reset - all attempts")

    client_mock = MagicMock()
    client_mock.post = _always_fails

    with patch.object(_pc, "_get_client", return_value=client_mock):
        with pytest.raises(BackendUnavailableError) as exc_info:
            await _pc._post(
                "/v1/chains/test-id/events",
                {"action_type": "tool_call"},
                action_type="tool_call",
            )

    assert exc_info.value.fail_mode == "allow"
    assert call_count == 4, (
        f"Expected 4 total attempts (max_retries=3), got {call_count}. "
        "ReadError must be retried the full configured count before giving up."
    )


@pytest.mark.asyncio
async def test_exhausted_read_error_retries_raise_backend_unavailable_on_deny():
    """
    Same exhausted-retry scenario with fail_mode='deny': BackendUnavailableError
    should be raised, not a bare ReadError.
    """
    _init_deny()

    async def _always_fails(*args, **kwargs) -> httpx.Response:
        raise httpx.ReadError("connection reset — all attempts")

    client_mock = MagicMock()
    client_mock.post = _always_fails

    with patch.object(_pc, "_get_client", return_value=client_mock):
        with pytest.raises(BackendUnavailableError):
            await _pc._post(
                "/v1/chains/test-id/events",
                {"action_type": "tool_call"},
                action_type="tool_call",
            )
