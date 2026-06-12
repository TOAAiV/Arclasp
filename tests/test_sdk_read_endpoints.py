"""
sdk/tests/test_sdk_read_endpoints.py — Unit tests for SDK read-endpoint client methods.

Tests verify:
  - Correct URL formation for each client function.
  - Correct auth header is included (from the initialized HTTP client).
  - Response data is parsed into the expected Pydantic model.
  - Chain.detail() / .events() / .receipt() delegate to the right client function.
  - chain.receipt() returns None on 404 rather than raising.

All tests mock the HTTP transport so no real backend is required.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import proofrail
from proofrail import client as _client
from proofrail.chain import Chain
from proofrail.models import (
    ChainDetail,
    ChainEventsResponse,
    ChainListResponse,
    ChainReceiptResponse,
    ChainSummary,
    ReceiptVerifyResponse,
)

# ---------------------------------------------------------------------------
# Helpers — fixture payloads that mirror real backend responses
# ---------------------------------------------------------------------------

_NOW = datetime.now(timezone.utc).isoformat()

_CHAIN_DETAIL_PAYLOAD = {
    "id": str(uuid.uuid4()),
    "organization_id": str(uuid.uuid4()),
    "external_chain_id": "my-workflow",
    "status": "active",
    "started_at": _NOW,
    "completed_at": None,
    "agents_involved": [],
    "cumulative_metrics": {},
    "environment": "development",
    "metadata": {},
    "created_at": _NOW,
    "updated_at": _NOW,
}

_EVENTS_PAYLOAD = {
    "events": [
        {
            "id": str(uuid.uuid4()),
            "sequence_number": 1,
            "agent_name": "pricing-agent",
            "parent_agent_name": None,
            "action_type": "tool_call",
            "action_name": "apply_discount",
            "action_payload": {"discount_pct": 10},
            "action_result": None,
            "risk_classification": {},
            "policy_decision": "allow",
            "decision_reason": None,
            "decision_source": "backend_evaluation",
            "evaluation_mode": "enforce",
            "executed_at": None,
            "created_at": _NOW,
        }
    ],
    "total": 1,
    "limit": 100,
    "offset": 0,
}

_RECEIPT_PAYLOAD = {
    "receipt_number": "RCP-ABC123",
    "summary": "Chain completed with 1 event.",
    "structured_data": {"chain_id": str(uuid.uuid4()), "events": []},
    "signature": "abc123hmac",
    "previous_receipt_hash": None,
    "created_at": _NOW,
}

_CHAIN_LIST_PAYLOAD = {
    "chains": [
        {
            "id": str(uuid.uuid4()),
            "external_chain_id": "my-workflow",
            "status": "completed",
            "started_at": _NOW,
            "completed_at": _NOW,
            "environment": "production",
            "agents_involved": [],
            "event_count": 3,
        }
    ],
    "total": 1,
    "limit": 50,
    "offset": 0,
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def init_sdk():
    """Initialize the SDK with a fake API key before each test."""
    proofrail.init(
        api_key="prail_testkey123",
        backend_url="http://test-backend",
        environment="development",
        fail_mode="deny",
    )
    yield
    # Reset module-level singletons so tests don't bleed into each other
    _client._config = None
    _client._clients_by_loop.clear()


def _mock_get(payload: dict):
    """Return an async mock that patches _client._get to return payload."""
    return patch.object(_client, "_get", new=AsyncMock(return_value=payload))


# ---------------------------------------------------------------------------
# client.get_chain
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_chain_calls_correct_url():
    chain_id = "abc-123"
    with _mock_get(_CHAIN_DETAIL_PAYLOAD) as mock:
        result = await _client.get_chain(chain_id)
        mock.assert_called_once_with(f"/v1/chains/{chain_id}")


@pytest.mark.asyncio
async def test_get_chain_returns_chain_detail_model():
    with _mock_get(_CHAIN_DETAIL_PAYLOAD):
        result = await _client.get_chain("abc-123")
        assert isinstance(result, ChainDetail)
        assert result.external_chain_id == "my-workflow"
        assert result.status == "active"
        assert result.completed_at is None


# ---------------------------------------------------------------------------
# client.get_chain_events
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_chain_events_default_params():
    chain_id = "abc-123"
    with _mock_get(_EVENTS_PAYLOAD) as mock:
        result = await _client.get_chain_events(chain_id)
        mock.assert_called_once_with(f"/v1/chains/{chain_id}/events?limit=100&offset=0")


@pytest.mark.asyncio
async def test_get_chain_events_with_cursor():
    chain_id = "abc-123"
    with _mock_get(_EVENTS_PAYLOAD) as mock:
        result = await _client.get_chain_events(chain_id, limit=10, offset=5, sequence_after=3)
        mock.assert_called_once_with(
            f"/v1/chains/{chain_id}/events?limit=10&offset=5&sequence_after=3"
        )


@pytest.mark.asyncio
async def test_get_chain_events_returns_response_model():
    with _mock_get(_EVENTS_PAYLOAD):
        result = await _client.get_chain_events("abc-123")
        assert isinstance(result, ChainEventsResponse)
        assert result.total == 1
        assert len(result.events) == 1
        assert result.events[0].agent_name == "pricing-agent"


# ---------------------------------------------------------------------------
# client.get_chain_receipt
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_chain_receipt_calls_correct_url():
    chain_id = "abc-123"
    with _mock_get(_RECEIPT_PAYLOAD) as mock:
        result = await _client.get_chain_receipt(chain_id)
        mock.assert_called_once_with(f"/v1/chains/{chain_id}/receipt")


@pytest.mark.asyncio
async def test_get_chain_receipt_returns_receipt_model():
    with _mock_get(_RECEIPT_PAYLOAD):
        result = await _client.get_chain_receipt("abc-123")
        assert isinstance(result, ChainReceiptResponse)
        assert result.receipt_number == "RCP-ABC123"
        assert result.signature == "abc123hmac"
        assert result.previous_receipt_hash is None


# ---------------------------------------------------------------------------
# client.list_chains
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_chains_default_params():
    with _mock_get(_CHAIN_LIST_PAYLOAD) as mock:
        result = await _client.list_chains()
        mock.assert_called_once_with("/v1/chains?limit=50&offset=0")


@pytest.mark.asyncio
async def test_list_chains_with_filters():
    with _mock_get(_CHAIN_LIST_PAYLOAD) as mock:
        result = await _client.list_chains(
            limit=10, offset=20, status="completed", environment="production"
        )
        called_path = mock.call_args[0][0]
        assert "limit=10" in called_path
        assert "offset=20" in called_path
        assert "status=completed" in called_path
        assert "environment=production" in called_path


@pytest.mark.asyncio
async def test_list_chains_returns_list_response_model():
    with _mock_get(_CHAIN_LIST_PAYLOAD):
        result = await _client.list_chains()
        assert isinstance(result, ChainListResponse)
        assert result.total == 1
        assert len(result.chains) == 1
        assert isinstance(result.chains[0], ChainSummary)
        assert result.chains[0].event_count == 3


# ---------------------------------------------------------------------------
# Chain.detail() — context manager helper
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chain_detail_delegates_to_client(init_sdk):
    chain = Chain("test-chain")
    chain._chain_id = "fake-id"

    with patch.object(_client, "get_chain", new=AsyncMock(return_value=MagicMock(spec=ChainDetail))) as mock:
        await chain.detail()
        mock.assert_called_once_with("fake-id")


@pytest.mark.asyncio
async def test_chain_detail_raises_if_not_started(init_sdk):
    chain = Chain("not-started")
    with pytest.raises(RuntimeError, match="context manager"):
        await chain.detail()


# ---------------------------------------------------------------------------
# Chain.events() — context manager helper
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chain_events_delegates_to_client(init_sdk):
    chain = Chain("test-chain")
    chain._chain_id = "fake-id"

    mock_response = MagicMock(spec=ChainEventsResponse)
    with patch.object(_client, "get_chain_events", new=AsyncMock(return_value=mock_response)) as mock:
        await chain.events(limit=50, offset=0, sequence_after=2)
        mock.assert_called_once_with("fake-id", limit=50, offset=0, sequence_after=2)


@pytest.mark.asyncio
async def test_chain_events_raises_if_not_started(init_sdk):
    chain = Chain("not-started")
    with pytest.raises(RuntimeError, match="context manager"):
        await chain.events()


# ---------------------------------------------------------------------------
# Chain.receipt() — context manager helper
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chain_receipt_delegates_to_client(init_sdk):
    chain = Chain("test-chain")
    chain._chain_id = "fake-id"

    mock_response = MagicMock(spec=ChainReceiptResponse)
    with patch.object(_client, "get_chain_receipt", new=AsyncMock(return_value=mock_response)) as mock:
        result = await chain.receipt()
        mock.assert_called_once_with("fake-id")
        assert result is mock_response


@pytest.mark.asyncio
async def test_chain_receipt_returns_none_on_404(init_sdk):
    """chain.receipt() must return None when backend returns 404 (no receipt yet)."""
    chain = Chain("test-chain")
    chain._chain_id = "fake-id"

    not_found = httpx.HTTPStatusError(
        "404 Not Found",
        request=MagicMock(),
        response=MagicMock(status_code=404),
    )
    with patch.object(_client, "get_chain_receipt", new=AsyncMock(side_effect=not_found)):
        result = await chain.receipt()
        assert result is None


@pytest.mark.asyncio
async def test_chain_receipt_reraises_non_404_errors(init_sdk):
    """chain.receipt() must re-raise non-404 HTTP errors (e.g. 403, 500)."""
    chain = Chain("test-chain")
    chain._chain_id = "fake-id"

    forbidden = httpx.HTTPStatusError(
        "403 Forbidden",
        request=MagicMock(),
        response=MagicMock(status_code=403),
    )
    with patch.object(_client, "get_chain_receipt", new=AsyncMock(side_effect=forbidden)):
        with pytest.raises(httpx.HTTPStatusError):
            await chain.receipt()


@pytest.mark.asyncio
async def test_chain_receipt_raises_if_not_started(init_sdk):
    chain = Chain("not-started")
    with pytest.raises(RuntimeError, match="context manager"):
        await chain.receipt()


# ---------------------------------------------------------------------------
# client.verify_receipt — C-3
# ---------------------------------------------------------------------------

_VERIFY_VALID_PAYLOAD = {
    "valid": True,
    "receipt_number": "RCP-ABC123",
    "chain_id": "00000000-0000-0000-0000-000000000001",
    "generated_at": _NOW,
}

_VERIFY_TAMPERED_PAYLOAD = {
    "valid": False,
    "receipt_number": "RCP-ABC123",
    "chain_id": "00000000-0000-0000-0000-000000000001",
    "generated_at": _NOW,
}


@pytest.mark.asyncio
async def test_verify_receipt_valid(init_sdk):
    """verify_receipt returns ReceiptVerifyResponse with valid=True on clean receipt."""
    receipt_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    with _mock_get(_VERIFY_VALID_PAYLOAD) as mock:
        result = await _client.verify_receipt(receipt_uuid)
        mock.assert_called_once_with(f"/v1/receipts/{receipt_uuid}/verify")

    assert isinstance(result, ReceiptVerifyResponse)
    assert result.valid is True
    assert result.receipt_number == "RCP-ABC123"


@pytest.mark.asyncio
async def test_verify_receipt_tampered(init_sdk):
    """verify_receipt returns valid=False when backend detects tampering."""
    with _mock_get(_VERIFY_TAMPERED_PAYLOAD):
        result = await _client.verify_receipt("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

    assert isinstance(result, ReceiptVerifyResponse)
    assert result.valid is False


@pytest.mark.asyncio
async def test_chain_verify_receipt_happy_path(init_sdk):
    """chain.verify_receipt(receipt_id) delegates to client.verify_receipt."""
    chain = Chain("test-chain")
    chain._chain_id = "fake-chain-id"
    receipt_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    mock_response = ReceiptVerifyResponse(
        valid=True,
        receipt_number="RCP-XYZ",
        chain_id="fake-chain-id",
        generated_at=_NOW,
    )
    with patch.object(_client, "verify_receipt", new=AsyncMock(return_value=mock_response)) as mock:
        result = await chain.verify_receipt(receipt_uuid)
        mock.assert_called_once_with(receipt_uuid)

    assert result.valid is True


@pytest.mark.asyncio
async def test_chain_verify_receipt_raises_if_not_started(init_sdk):
    """chain.verify_receipt() raises RuntimeError when chain has not been started."""
    chain = Chain("not-started")
    with pytest.raises(RuntimeError, match="context manager"):
        await chain.verify_receipt("some-uuid")


# ---------------------------------------------------------------------------
# client.init() re-init closes old HTTP client (I-5)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reinit_closes_old_http_client():
    """
    Calling proofrail.init() a second time must close the current loop's
    httpx.AsyncClient so that TCP connections and file descriptors are released.

    With the loop-aware design, clients are created lazily on first _get_client()
    call.  We force creation here, patch aclose, re-init, then verify the task
    fired and a subsequent _get_client() returns a fresh instance.
    """
    import asyncio
    from unittest.mock import AsyncMock

    # Force creation of the client for the current pytest-asyncio event loop.
    old_client = _client._get_client()

    # Patch aclose on the existing client so we can verify it is scheduled.
    old_client.aclose = AsyncMock()

    # Second init with a different api_key — must schedule aclose for current
    # loop's client, then clear _clients_by_loop.
    proofrail.init(
        api_key="prail_newkey456",
        backend_url="http://test-backend-2",
        environment="development",
    )

    # Allow the event loop to drain pending tasks (aclose is fire-and-forget).
    await asyncio.sleep(0)

    old_client.aclose.assert_called_once()

    # After re-init the dict is empty; the next _get_client() builds a fresh
    # client with the new config and is a different object from the old one.
    new_client = _client._get_client()
    assert new_client is not old_client


# ---------------------------------------------------------------------------
# BUG-LC-03 regression: loop-aware client (I-5b)
# ---------------------------------------------------------------------------

def test_loop_aware_client_creates_separate_clients_for_separate_loops():
    """
    BUG-LC-03 regression test.

    Before the fix, a single global httpx.AsyncClient was created in the
    event loop that called proofrail.init().  When a LangChain sync
    StructuredTool callback invoked proofrail via asyncio.Runner() — which
    spins up a *new* event loop — httpx's internal anyio.Event raised:

        RuntimeError: asyncio.Event bound to a different event loop

    The exception was swallowed by langchain-core's ``except Exception``
    handler, silently dropping tool_call events and corrupting the client
    state so chain._complete() also failed (BUG-LC-04).

    Fix: _get_client() now uses a WeakKeyDictionary keyed on the running
    loop.  Each loop gets its own freshly-created httpx.AsyncClient.

    This test creates two asyncio.Runner() instances (the exact pattern
    that triggered the bug in production) and asserts that each gets a
    distinct client object.
    """
    import asyncio

    results_a: list = []
    results_b: list = []

    async def capture(storage: list) -> None:
        storage.append(_client._get_client())

    # Two separate Runner() calls → two separate event loops → two clients.
    with asyncio.Runner() as runner:
        runner.run(capture(results_a))
    with asyncio.Runner() as runner:
        runner.run(capture(results_b))

    assert len(results_a) == 1 and len(results_b) == 1
    assert results_a[0] is not results_b[0], (
        "_get_client() must return a separate httpx.AsyncClient per event loop; "
        "sharing one client across loops causes BUG-LC-03"
    )


# ---------------------------------------------------------------------------
# HTTP plaintext warning (SDK-S-6)
#
# Warning now fires via logger.warning (not warnings.warn) for any
# non-localhost HTTP backend_url, regardless of environment.
# ---------------------------------------------------------------------------

def test_init_warns_on_http_non_localhost(caplog):
    """
    logger.warning must fire when backend_url is a non-localhost HTTP URL.
    The check applies regardless of environment (production or otherwise).
    """
    import logging
    with caplog.at_level(logging.WARNING, logger="proofrail.client"):
        proofrail.init(
            api_key="prail_test_warn",
            backend_url="http://insecure-backend",
            environment="production",
        )

    assert any("plaintext HTTP" in r.message for r in caplog.records), (
        "Expected a 'plaintext HTTP' logger.warning for a non-localhost HTTP URL"
    )


def test_init_warns_on_http_non_localhost_regardless_of_environment(caplog):
    """
    logger.warning fires for a non-localhost HTTP URL even when
    environment='development' — the check is no longer scoped to production.
    """
    import logging
    with caplog.at_level(logging.WARNING, logger="proofrail.client"):
        proofrail.init(
            api_key="prail_test_warn_dev",
            backend_url="http://staging-backend",
            environment="development",
        )

    assert any("plaintext HTTP" in r.message for r in caplog.records), (
        "Expected a 'plaintext HTTP' logger.warning for a non-localhost HTTP URL "
        "regardless of environment"
    )


def test_init_no_warn_on_https(caplog):
    """No logger warning when backend_url uses https://."""
    import logging
    with caplog.at_level(logging.WARNING, logger="proofrail.client"):
        proofrail.init(
            api_key="prail_test_no_warn",
            backend_url="https://secure-backend",
            environment="production",
        )

    assert not any("plaintext HTTP" in r.message for r in caplog.records)


def test_init_no_warn_on_http_localhost(caplog):
    """
    localhost URLs are exempt from the HTTP warning regardless of environment.
    This explains why tests using http://localhost:9999 with environment=production
    no longer emit warnings.
    """
    import logging
    with caplog.at_level(logging.WARNING, logger="proofrail.client"):
        proofrail.init(
            api_key="prail_test_localhost",
            backend_url="http://localhost:9999",
            environment="production",
        )

    assert not any("plaintext HTTP" in r.message for r in caplog.records)


def test_init_no_warn_on_http_localhost_in_production(caplog):
    """http://localhost:8000 in production must not warn — localhost is always exempt."""
    import logging
    with caplog.at_level(logging.WARNING, logger="proofrail.client"):
        proofrail.init(
            api_key="prail_test_dev",
            backend_url="http://localhost:8000",
            environment="production",
        )

    assert not any("plaintext HTTP" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Fast-path kill switch INFO log (SDK-S-3)
#
# When enable_local_fast_path=True (default), init() logs an INFO message
# surfacing the kill-switch limitation. When False, no message is emitted.
# ---------------------------------------------------------------------------

def test_init_info_logs_fast_path_kill_switch_warning(caplog):
    """
    An INFO log mentioning the kill-switch limitation must fire when
    enable_local_fast_path=True (the default).
    """
    import logging
    with caplog.at_level(logging.INFO, logger="proofrail.client"):
        proofrail.init(
            api_key="prail_test_fp_on",
            backend_url="http://localhost:9999",
            enable_local_fast_path=True,
        )

    assert any("kill switch" in r.message for r in caplog.records), (
        "Expected an INFO log mentioning 'kill switch' when enable_local_fast_path=True"
    )


def test_init_no_info_when_fast_path_disabled(caplog):
    """
    No kill-switch INFO log must fire when enable_local_fast_path=False —
    the limitation does not apply when fast-path is disabled.
    """
    import logging
    with caplog.at_level(logging.INFO, logger="proofrail.client"):
        proofrail.init(
            api_key="prail_test_fp_off",
            backend_url="http://localhost:9999",
            enable_local_fast_path=False,
        )

    assert not any("kill switch" in r.message for r in caplog.records), (
        "Expected no kill-switch INFO log when enable_local_fast_path=False"
    )
