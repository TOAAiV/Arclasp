"""
Tests for the offline / fail_mode=allow code path — Item D verification.

Verifies:
- Chain.__enter__ does NOT KeyError when backend is unreachable (bug #4/#5)
- self._offline is True after backend failure
- record_agent_action buffers events and returns allow
- fail_mode=deny raises BackendUnavailableError instead
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

import proofrail
from proofrail.chain import Chain, _buffer_event
from proofrail.exceptions import BackendUnavailableError
from proofrail.models import PolicyDecision


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

@pytest.fixture()
def sdk_allow():
    proofrail.init(
        api_key="prail_test",
        backend_url="http://localhost:9999",
        fail_mode="allow",
    )


@pytest.fixture()
def sdk_deny():
    proofrail.init(
        api_key="prail_test",
        backend_url="http://localhost:9999",
        fail_mode="deny",
    )


def _connect_error(*args, **kwargs):
    raise httpx.ConnectError("Connection refused")


# ---------------------------------------------------------------------------
# Tests: fail_mode=allow
# ---------------------------------------------------------------------------

class TestOfflineAllow:
    @pytest.mark.asyncio
    async def test_enter_does_not_key_error(self, sdk_allow):
        """Bug #4/#5 regression: _start() must not KeyError on offline payload."""
        with patch("proofrail.client._get_client") as mock_get_client:
            mock_client = mock_get_client.return_value
            mock_client.post = _connect_error

            async with Chain("test") as chain:
                pass  # must not raise

    @pytest.mark.asyncio
    async def test_offline_flag_set_after_backend_failure(self, sdk_allow):
        with patch("proofrail.client._get_client") as mock_get_client:
            mock_client = mock_get_client.return_value
            mock_client.post = _connect_error

            async with Chain("test") as chain:
                assert chain._offline is True

    @pytest.mark.asyncio
    async def test_chain_id_assigned_offline(self, sdk_allow):
        """A local UUID must be generated so chain_id is never None."""
        with patch("proofrail.client._get_client") as mock_get_client:
            mock_client = mock_get_client.return_value
            mock_client.post = _connect_error

            async with Chain("test") as chain:
                assert chain.chain_id is not None
                assert len(chain.chain_id) == 36  # UUID format

    @pytest.mark.asyncio
    async def test_record_agent_action_returns_allow_offline(self, sdk_allow):
        with patch("proofrail.client._get_client") as mock_get_client:
            mock_client = mock_get_client.return_value
            mock_client.post = _connect_error

            async with Chain("test") as chain:
                result = await chain.record_agent_action(
                    agent_name="agent",
                    action_type="tool_call",
                    action_name="do_something",
                )

        assert isinstance(result, PolicyDecision)
        assert result.policy_decision == "allow"
        assert result.decision_source == "offline_stub"

    @pytest.mark.asyncio
    async def test_events_buffered_offline(self, sdk_allow):
        with patch("proofrail.client._get_client") as mock_get_client:
            mock_client = mock_get_client.return_value
            mock_client.post = _connect_error

            async with Chain("test") as chain:
                await chain.record_agent_action(
                    agent_name="agent", action_type="tool_call", action_name="a1"
                )
                await chain.record_agent_action(
                    agent_name="agent", action_type="tool_call", action_name="a2"
                )
                await chain.record_agent_action(
                    agent_name="agent", action_type="tool_call", action_name="a3"
                )

        assert len(chain._offline_buffer) == 3
        action_names = [e["action_name"] for e in chain._offline_buffer]
        assert action_names == ["a1", "a2", "a3"]

    @pytest.mark.asyncio
    async def test_multiple_records_dont_raise(self, sdk_allow):
        with patch("proofrail.client._get_client") as mock_get_client:
            mock_client = mock_get_client.return_value
            mock_client.post = _connect_error

            async with Chain("test") as chain:
                for i in range(5):
                    result = await chain.record_agent_action(
                        agent_name="agent",
                        action_type="tool_call",
                        action_name=f"action_{i}",
                    )
                    assert result.policy_decision == "allow"


# ---------------------------------------------------------------------------
# Tests: fail_mode=deny
# ---------------------------------------------------------------------------

class TestOfflineDeny:
    @pytest.mark.asyncio
    async def test_deny_raises_backend_unavailable(self, sdk_deny):
        """With fail_mode=deny, backend failure on chain start raises the right error."""
        with patch("proofrail.client._get_client") as mock_get_client:
            mock_client = mock_get_client.return_value
            mock_client.post = _connect_error

            with pytest.raises(BackendUnavailableError) as exc_info:
                async with Chain("test"):
                    pass

        assert exc_info.value.fail_mode == "deny"

    @pytest.mark.asyncio
    async def test_deny_error_has_correct_fail_mode(self, sdk_deny):
        with patch("proofrail.client._get_client") as mock_get_client:
            mock_client = mock_get_client.return_value
            mock_client.post = _connect_error

            with pytest.raises(BackendUnavailableError) as exc_info:
                async with Chain("test"):
                    pass

        assert "deny" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Tests: _buffer_event helper (callable independently)
# ---------------------------------------------------------------------------

class TestBufferEventHelper:
    def test_buffers_event(self):
        buf = []
        event = {"action_name": "x", "agent_name": "a"}
        result = _buffer_event(buf, event, max_events=10)
        assert result is True
        assert buf == [event]

    def test_drops_oldest_when_full(self):
        """When full, oldest event is evicted so the new event is always accepted."""
        buf = [{"action_name": f"action_{i}"} for i in range(3)]
        oldest = buf[0].copy()
        result = _buffer_event(buf, {"action_name": "new_event"}, max_events=3)
        assert result is True          # always accepted now
        assert len(buf) == 3           # still at capacity
        assert oldest not in buf       # oldest was evicted
        assert buf[-1] == {"action_name": "new_event"}  # new event appended

    def test_callable_without_chain(self):
        """Verify the helper is a standalone function importable for Phase 4 use."""
        from proofrail.chain import _buffer_event as imported_fn
        assert callable(imported_fn)
        buf = []
        imported_fn(buf, {"test": "event"}, max_events=5)
        assert len(buf) == 1
