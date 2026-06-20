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

  3. test_idempotency_key_persists_through_offline_buffer_drain
       When the backend signals offline (_OfflineSignal), the event is
       buffered with its idempotency_key embedded.  When the drain later
       sends that buffered event, it sends the same key.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, Mock, patch

import httpx
import pytest

import proofrail
from proofrail.chain import Chain, _drain_offline_buffer
from proofrail.client import _OfflineSignal


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
    proofrail.init(
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
            return _ALLOW_DECISION if "events" in path else _CHAIN_RESPONSE

        with patch("proofrail.client._post", side_effect=fake_post):
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

        with patch("proofrail.client._get_client") as mock_get_client:
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


class TestIdempotencyKeyOfflineDrain:
    @pytest.mark.asyncio
    async def test_idempotency_key_persists_through_offline_buffer_drain(self):
        """
        The idempotency_key embedded in event_body when the event is buffered
        offline is the same key sent during the drain.
        """
        proofrail.init(
            api_key="prail_test",
            backend_url="http://test",
            fail_mode="allow",
            enable_local_fast_path=False,
            max_retries=0,
        )

        # Manually start chain in online state to get a real chain_id
        chain = Chain("test")
        chain._chain_id = "offline-drain-test"
        chain._offline = False

        # Step 1: trigger offline transition via _OfflineSignal
        with patch("proofrail.client._post", side_effect=_OfflineSignal("backend down")):
            result = await chain.record_agent_action(
                agent_name="a", action_type="tool_call", action_name="drain_test"
            )

        # Cancel the drain task created during the offline transition
        # (it would fail and sleep because _post is still patched to raise)
        if chain._drain_task and not chain._drain_task.done():
            chain._drain_task.cancel()
            try:
                await chain._drain_task
            except asyncio.CancelledError:
                pass

        assert result.decision_source == "offline_stub"
        assert len(chain._offline_buffer) == 1, (
            f"Expected 1 buffered event, got {len(chain._offline_buffer)}"
        )

        key_in_buffer = chain._offline_buffer[0].get("idempotency_key")
        assert key_in_buffer is not None, "Buffered event must contain idempotency_key"
        assert len(key_in_buffer) == 32, f"Key must be 32-char hex, len={len(key_in_buffer)}"

        # Step 2: drain with a working _post — capture what was sent
        drain_bodies: list[dict] = []

        async def capture_drain(path, data, action_type=None):
            if "events" in path:
                drain_bodies.append(dict(data))
            return _ALLOW_DECISION

        with patch("proofrail.client._post", side_effect=capture_drain):
            chain._drain_task = asyncio.create_task(_drain_offline_buffer(chain))
            await asyncio.wait_for(chain._drain_task, timeout=5.0)

        assert len(drain_bodies) == 1, f"Expected 1 drain POST, got {len(drain_bodies)}"

        key_in_drain = drain_bodies[0].get("idempotency_key")
        assert key_in_drain == key_in_buffer, (
            f"Drain key {key_in_drain!r} must match the key embedded at buffer time "
            f"{key_in_buffer!r}"
        )
