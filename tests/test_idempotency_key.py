"""
Tests for idempotency key generation in record_agent_action (BUG-CC-01).

Three tests:

  1. test_idempotency_key_generated_per_event
       Two separate record_agent_action calls each receive a distinct
       32-char hex idempotency_key.

  2. test_idempotency_key_persists_through_retry
       When the underlying httpx client raises TimeoutException on the first
       attempt, _retry_with_backoff retries using the same data dict.
       Both attempts carry the same idempotency_key.

  3. test_idempotency_key_persists_until_exhausted_failure
       When all transport retries fail, each wire attempt carries the same
       idempotency_key and no offline buffer is created.
"""

from __future__ import annotations

from unittest.mock import MagicMock, Mock, patch

import httpx
import pytest

import arclasp
from arclasp.chain import Chain
from arclasp.exceptions import BackendUnavailableError


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _mock_response(status_code: int, json_data: dict) -> Mock:
    """Minimal httpx.Response stand-in for use with _get_client() mocks."""
    m = Mock()
    m.status_code = status_code
    m.headers = {}
    m.raise_for_status = Mock()
    m.json = Mock(return_value=json_data)
    return m


_ALLOW_DECISION = {"policy_decision": "allow", "decision_source": "backend_evaluation"}
_CHAIN_RESPONSE = {"id": "test-chain-001"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def sdk_init():
    arclasp.init(
        api_key="prail_test",
        backend_url="http://test",
        fail_mode="deny",
        enable_local_fast_path=False,  # ensure all events go through _post
        max_retries=1,
        retry_backoff_base_ms=0,       # no sleep between retries in tests
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestIdempotencyKeyGeneration:
    @pytest.mark.asyncio
    async def test_idempotency_key_generated_per_event(self):
        """Each record_agent_action call gets a unique 32-char hex key."""
        captured = []

        async def fake_post(path, data, action_type=None):
            if "events" in path:
                captured.append(data.get("idempotency_key"))
                return _ALLOW_DECISION
            if path.endswith("/complete"):
                return {"status": "completed"}
            return _ALLOW_DECISION if "events" in path else _CHAIN_RESPONSE

        with patch("arclasp.client._post", side_effect=fake_post):
            async with Chain("test") as chain:
                await chain.record_agent_action(
                    agent_name="a", action_type="tool_call", action_name="action_1"
                )
                await chain.record_agent_action(
                    agent_name="a", action_type="tool_call", action_name="action_2"
                )

        assert len(captured) == 2, f"Expected 2 event POSTs, got {len(captured)}"

        k1, k2 = captured
        assert k1 is not None, "First event must have idempotency_key"
        assert k2 is not None, "Second event must have idempotency_key"
        assert k1 != k2, "Different events must have different idempotency_keys"
        assert len(k1) == 32, f"Key must be 32-char hex, got length {len(k1)}: {k1!r}"
        assert len(k2) == 32, f"Key must be 32-char hex, got length {len(k2)}: {k2!r}"
        # Confirm format: valid hex
        int(k1, 16)
        int(k2, 16)


class TestIdempotencyKeyRetryPersistence:
    @pytest.mark.asyncio
    async def test_idempotency_key_persists_through_retry(self):
        """
        When _retry_with_backoff retries a timed-out event POST, the same
        idempotency_key is sent on both the first and second wire attempts.
        """
        event_post_bodies: list[dict] = []

        async def fake_client_post(path, json=None, **kwargs):
            if not path.endswith("/events"):
                # Chain create always succeeds
                return _mock_response(201, _CHAIN_RESPONSE)
            # Event POST: fail first, succeed on retry
            event_post_bodies.append(dict(json) if json else {})
            if len(event_post_bodies) == 1:
                raise httpx.TimeoutException("simulated first-attempt timeout")
            return _mock_response(201, _ALLOW_DECISION)

        with patch("arclasp.client._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.post = fake_client_post
            mock_get_client.return_value = mock_client

            chain = Chain("test")
            await chain._start()

            await chain.record_agent_action(
                agent_name="a", action_type="tool_call", action_name="retry_action"
            )

        assert len(event_post_bodies) == 2, (
            f"Expected 2 event POST attempts (initial + retry), "
            f"got {len(event_post_bodies)}"
        )

        k1 = event_post_bodies[0].get("idempotency_key")
        k2 = event_post_bodies[1].get("idempotency_key")

        assert k1 is not None, "Initial attempt must include idempotency_key"
        assert k2 is not None, "Retry attempt must include idempotency_key"
        assert k1 == k2, (
            f"Retry must carry the same key as the original attempt: "
            f"{k1!r} vs {k2!r}"
        )
        assert len(k1) == 32


class TestIdempotencyKeyFailClosedFailure:
    @pytest.mark.asyncio
    async def test_idempotency_key_persists_until_exhausted_failure(self):
        """
        The same idempotency_key is sent on every retry attempt, and a final
        backend failure raises instead of buffering a local allow.
        """
        arclasp.init(
            api_key="prail_test",
            backend_url="http://test",
            fail_mode="deny",
            enable_local_fast_path=False,
            max_retries=1,
            retry_backoff_base_ms=0,
        )

        event_post_bodies: list[dict] = []

        async def fake_client_post(path, json=None, **kwargs):
            if not path.endswith("/events"):
                return _mock_response(201, _CHAIN_RESPONSE)
            event_post_bodies.append(dict(json) if json else {})
            raise httpx.TimeoutException("simulated exhausted timeout")

        with patch("arclasp.client._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.post = fake_client_post
            mock_get_client.return_value = mock_client

            chain = Chain("test")
            await chain._start()

            with pytest.raises(BackendUnavailableError):
                await chain.record_agent_action(
                    agent_name="a",
                    action_type="tool_call",
                    action_name="fail_closed_action",
                )

        assert len(event_post_bodies) == 2
        keys = [body.get("idempotency_key") for body in event_post_bodies]
        assert keys[0] is not None
        assert keys[0] == keys[1]
        assert len(keys[0]) == 32
        assert chain._offline is False
        assert chain._offline_buffer == []
