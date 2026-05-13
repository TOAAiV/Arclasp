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
        fail_mode="deny",
    )
    yield
    # Reset module-level singletons so tests don't bleed into each other
    _client._config = None
    _client._http_client = None


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
    Calling proofrail.init() a second time must close the old httpx.AsyncClient
    so that TCP connections and file descriptors are released.
    """
    import asyncio
    from unittest.mock import AsyncMock, patch

    # First init — already done by the autouse fixture (init_sdk).
    old_client = _client._http_client
    assert old_client is not None

    # Patch aclose on the *existing* client instance so we can verify it is called.
    old_client.aclose = AsyncMock()

    # Second init with a different api_key — should schedule close of old_client.
    proofrail.init(
        api_key="prail_newkey456",
        backend_url="http://test-backend-2",
    )

    # Allow the event loop to run pending tasks (aclose was fire-and-forget).
    await asyncio.sleep(0)

    old_client.aclose.assert_called_once()
    assert _client._http_client is not old_client
