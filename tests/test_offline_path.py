"""
K1 fail-closed regression tests for the former offline / fail_mode=allow path.

``fail_mode="allow"`` is retained for signature compatibility but no longer
allows governed SDK execution without an authoritative backend response.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

import arclasp
from arclasp.chain import Chain, _buffer_event
from arclasp.exceptions import BackendUnavailableError


@pytest.fixture()
def sdk_allow():
    with pytest.warns(DeprecationWarning, match="fail_mode"):
        arclasp.init(
            api_key="prail_test",
            backend_url="http://localhost:9999",
            fail_mode="allow",
            max_retries=0,
        )


@pytest.fixture()
def sdk_deny():
    arclasp.init(
        api_key="prail_test",
        backend_url="http://localhost:9999",
        fail_mode="deny",
        max_retries=0,
    )


def _connect_error(*args, **kwargs):
    raise httpx.ConnectError("Connection refused")


class TestFailClosedAllow:
    @pytest.mark.asyncio
    async def test_chain_start_allow_raises_backend_unavailable_not_offline(self, sdk_allow):
        with patch("arclasp.client._get_client") as mock_get_client:
            mock_client = mock_get_client.return_value
            mock_client.post = _connect_error

            chain = Chain("test")
            with pytest.raises(BackendUnavailableError) as exc_info:
                await chain._start()

        assert exc_info.value.fail_mode == "allow"
        assert chain.chain_id is None
        assert chain._offline is False
        assert chain._offline_buffer == []

    @pytest.mark.asyncio
    async def test_event_allow_raises_backend_unavailable_not_buffered(self, sdk_allow):
        chain = Chain("test")
        chain._chain_id = "authoritative-chain-id"

        with patch("arclasp.client._get_client") as mock_get_client:
            mock_client = mock_get_client.return_value
            mock_client.post = _connect_error

            with pytest.raises(BackendUnavailableError) as exc_info:
                await chain.record_agent_action(
                    agent_name="agent",
                    action_type="tool_call",
                    action_name="do_something",
                )

        assert exc_info.value.fail_mode == "allow"
        assert chain._offline is False
        assert chain._offline_buffer == []

    @pytest.mark.asyncio
    async def test_no_public_offline_chain_context_manager(self, sdk_allow):
        with patch("arclasp.client._get_client") as mock_get_client:
            mock_client = mock_get_client.return_value
            mock_client.post = _connect_error

            with pytest.raises(BackendUnavailableError):
                async with Chain("test"):
                    raise AssertionError("body must not run without backend authority")


class TestOfflineDeny:
    @pytest.mark.asyncio
    async def test_deny_raises_backend_unavailable(self, sdk_deny):
        with patch("arclasp.client._get_client") as mock_get_client:
            mock_client = mock_get_client.return_value
            mock_client.post = _connect_error

            with pytest.raises(BackendUnavailableError) as exc_info:
                async with Chain("test"):
                    pass

        assert exc_info.value.fail_mode == "deny"

    @pytest.mark.asyncio
    async def test_deny_error_has_correct_fail_mode(self, sdk_deny):
        with patch("arclasp.client._get_client") as mock_get_client:
            mock_client = mock_get_client.return_value
            mock_client.post = _connect_error

            with pytest.raises(BackendUnavailableError) as exc_info:
                async with Chain("test"):
                    pass

        assert "deny" in str(exc_info.value)


class TestFailModesChainStart:
    @pytest.mark.asyncio
    async def test_default_key_allow_still_fails_closed(self):
        with pytest.warns(DeprecationWarning, match="fail_mode"):
            arclasp.init(
                api_key="prail_test",
                backend_url="http://localhost:9999",
                fail_mode="deny",
                fail_modes={"default": "allow"},
                max_retries=0,
            )
        with patch("arclasp.client._get_client") as mock_get_client:
            mock_client = mock_get_client.return_value
            mock_client.post = _connect_error

            with pytest.raises(BackendUnavailableError) as exc_info:
                async with Chain("test"):
                    pass

        assert exc_info.value.fail_mode == "allow"

    @pytest.mark.asyncio
    async def test_chain_create_key_allow_still_fails_closed(self):
        with pytest.warns(DeprecationWarning, match="fail_mode"):
            arclasp.init(
                api_key="prail_test",
                backend_url="http://localhost:9999",
                fail_mode="deny",
                fail_modes={"chain_create": "allow"},
                max_retries=0,
            )
        with patch("arclasp.client._get_client") as mock_get_client:
            mock_client = mock_get_client.return_value
            mock_client.post = _connect_error

            with pytest.raises(BackendUnavailableError) as exc_info:
                async with Chain("test"):
                    pass

        assert exc_info.value.fail_mode == "allow"

    @pytest.mark.asyncio
    async def test_tool_call_only_does_not_affect_chain_start(self):
        with pytest.warns(DeprecationWarning, match="fail_mode"):
            arclasp.init(
                api_key="prail_test",
                backend_url="http://localhost:9999",
                fail_mode="deny",
                fail_modes={"tool_call": "allow"},
                max_retries=0,
            )
        with patch("arclasp.client._get_client") as mock_get_client:
            mock_client = mock_get_client.return_value
            mock_client.post = _connect_error

            with pytest.raises(BackendUnavailableError) as exc_info:
                async with Chain("test"):
                    pass

        assert exc_info.value.fail_mode == "deny"

    @pytest.mark.asyncio
    async def test_empty_fail_modes_preserves_global_deny(self):
        arclasp.init(
            api_key="prail_test",
            backend_url="http://localhost:9999",
            fail_mode="deny",
            fail_modes={},
            max_retries=0,
        )
        with patch("arclasp.client._get_client") as mock_get_client:
            mock_client = mock_get_client.return_value
            mock_client.post = _connect_error

            with pytest.raises(BackendUnavailableError) as exc_info:
                async with Chain("test"):
                    pass

        assert exc_info.value.fail_mode == "deny"


class TestBufferEventHelper:
    def test_buffers_event(self):
        buf = []
        event = {"action_name": "x", "agent_name": "a"}
        result = _buffer_event(buf, event, max_events=10)
        assert result is True
        assert buf == [event]

    def test_drops_oldest_when_full(self):
        buf = [{"action_name": f"action_{i}"} for i in range(3)]
        oldest = buf[0].copy()
        result = _buffer_event(buf, {"action_name": "new_event"}, max_events=3)
        assert result is True
        assert len(buf) == 3
        assert oldest not in buf
        assert buf[-1] == {"action_name": "new_event"}

    def test_callable_without_chain(self):
        from arclasp.chain import _buffer_event as imported_fn

        assert callable(imported_fn)
        buf = []
        imported_fn(buf, {"test": "event"}, max_events=5)
        assert len(buf) == 1
