"""Fail-closed regression tests for backend-unavailable paths."""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
from pydantic import ValidationError

import arclasp
from arclasp.chain import Chain, _buffer_event
from arclasp.exceptions import BackendUnavailableError
from arclasp.models import ChainConfig


@pytest.fixture()
def sdk_configured():
    arclasp.init(
        api_key="prail_test",
        backend_url="http://localhost:9999",
        max_retries=0,
    )


def _connect_error(*args, **kwargs):
    raise httpx.ConnectError("Connection refused")


class TestFailClosed:
    @pytest.mark.asyncio
    async def test_chain_start_raises_backend_unavailable_not_offline(self, sdk_configured):
        with patch("arclasp.client._get_client") as mock_get_client:
            mock_client = mock_get_client.return_value
            mock_client.post = _connect_error

            chain = Chain("test")
            with pytest.raises(BackendUnavailableError):
                await chain._start()

        assert chain.chain_id is None
        assert chain._offline is False
        assert chain._offline_buffer == []

    @pytest.mark.asyncio
    async def test_event_raises_backend_unavailable_not_buffered(self, sdk_configured):
        chain = Chain("test")
        chain._chain_id = "authoritative-chain-id"

        with patch("arclasp.client._get_client") as mock_get_client:
            mock_client = mock_get_client.return_value
            mock_client.post = _connect_error

            with pytest.raises(BackendUnavailableError):
                await chain.record_agent_action(
                    agent_name="agent",
                    action_type="tool_call",
                    action_name="do_something",
                )

        assert chain._offline is False
        assert chain._offline_buffer == []

    @pytest.mark.asyncio
    async def test_no_public_offline_chain_context_manager(self, sdk_configured):
        with patch("arclasp.client._get_client") as mock_get_client:
            mock_client = mock_get_client.return_value
            mock_client.post = _connect_error

            with pytest.raises(BackendUnavailableError):
                async with Chain("test"):
                    raise AssertionError("body must not run without backend authority")


class TestRemovedConfigKnobs:
    @pytest.mark.parametrize("kwargs", [
        {"fail_mode": "allow"},
        {"fail_mode": "deny"},
        {"fail_modes": {"default": "allow"}},
        {"enable_local_fast_path": True},
        {"enable_local_fast_path": False},
    ])
    def test_obsolete_config_keys_are_rejected(self, kwargs):
        with pytest.raises(ValidationError):
            ChainConfig(api_key="prail_test", **kwargs)

    def test_init_rejects_obsolete_config_keys(self):
        with pytest.raises(ValidationError):
            arclasp.init(api_key="prail_test", fail_mode="allow")


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
