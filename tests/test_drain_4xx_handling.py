"""
Tests for BUG-CC-02: _drain_offline_buffer 4xx error classification.

Three tests verify the fixed drain loop:

  1. test_drain_skips_permanent_4xx
       5 buffered events; event[3] gets a 422 response.
       drain discards event[3] with a WARNING and delivers the other 4.
       Buffer ends up empty; chain recovers to online.

  2. test_drain_breaks_on_transient_503_then_retries
       3 buffered events; event[1] gets a 503 on the first drain pass.
       drain breaks, sleeps (mocked), retries; all 3 land on the second pass.

  3. test_drain_breaks_on_429_then_retries
       Same pattern with 429 (rate limit).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import arclasp
from arclasp.chain import Chain, _drain_offline_buffer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ALLOW_DECISION = {"policy_decision": "allow", "decision_source": "backend_evaluation"}


def _make_http_error(status_code: int, body: str = "") -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError(
        f"{status_code} Error",
        request=MagicMock(),
        response=MagicMock(status_code=status_code, text=body),
    )


def _make_chain(chain_id: str = "drain-test-chain") -> Chain:
    """Return a Chain already started and in offline mode."""
    chain = Chain("test")
    chain._chain_id = chain_id
    chain._offline = True
    return chain


def _buffered_event(i: int) -> dict:
    return {
        "action_name": f"action_{i}",
        "action_type": "tool_call",
        "idempotency_key": uuid.uuid4().hex,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def sdk_init():
    arclasp.init(
        api_key="prail_test",
        backend_url="http://test",
        fail_mode="allow",
        enable_local_fast_path=False,
        max_retries=0,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDrainPermanent4xx:
    @pytest.mark.asyncio
    async def test_drain_skips_permanent_4xx(self, caplog):
        """
        When event[3] returns 422, drain discards it with a WARNING and
        continues to deliver events 0-2 and 4. Buffer ends up empty and
        chain recovers to online.
        """
        chain = _make_chain()
        for i in range(5):
            chain._offline_buffer.append(_buffered_event(i))

        posts: list[str] = []

        async def mock_post(path, body, action_type=None):
            if "events" not in path:
                return {}
            action_name = (body or {}).get("action_name", "")
            posts.append(action_name)
            if action_name == "action_3":
                raise _make_http_error(422, "Unprocessable entity")
            return _ALLOW_DECISION

        with caplog.at_level(logging.WARNING, logger="arclasp.chain"), \
             patch("arclasp.client._post", side_effect=mock_post):
            chain._drain_task = asyncio.create_task(_drain_offline_buffer(chain))
            await asyncio.wait_for(chain._drain_task, timeout=5.0)

        # All 5 events attempted; no retry needed (422 is permanent — discard and continue).
        assert posts == ["action_0", "action_1", "action_2", "action_3", "action_4"], (
            f"Unexpected POST sequence: {posts}"
        )
        assert len(chain._offline_buffer) == 0, (
            f"Buffer should be empty; still contains: {chain._offline_buffer}"
        )
        assert chain._offline is False, "Chain should recover to online after full drain"

        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warning_records) == 1, (
            f"Expected exactly 1 WARNING for discarded event; got: "
            f"{[r.getMessage() for r in warning_records]}"
        )
        msg = warning_records[0].getMessage()
        assert "action_3" in msg, f"WARNING must name the discarded action; got: {msg!r}"
        assert "422" in msg, f"WARNING must include status code; got: {msg!r}"


class TestDrainTransientRetry:
    @pytest.mark.asyncio
    async def test_drain_breaks_on_transient_503_then_retries(self):
        """
        event[1] raises 503 on the first drain pass. drain breaks the inner
        loop, sleeps (mocked to instant), then retries; all 3 land on the
        second pass.
        """
        chain = _make_chain()
        for i in range(3):
            chain._offline_buffer.append(_buffered_event(i))

        first_action1_attempt = True
        posts: list[str] = []

        async def mock_post(path, body, action_type=None):
            nonlocal first_action1_attempt
            if "events" not in path:
                return {}
            action_name = (body or {}).get("action_name", "")
            posts.append(action_name)
            if action_name == "action_1" and first_action1_attempt:
                first_action1_attempt = False
                raise _make_http_error(503, "Service unavailable")
            return _ALLOW_DECISION

        with patch("arclasp.client._post", side_effect=mock_post), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            chain._drain_task = asyncio.create_task(_drain_offline_buffer(chain))
            await asyncio.wait_for(chain._drain_task, timeout=5.0)

        # Pass 1: action_0 ok, action_1 → 503 (break inner loop)
        # Pass 2: action_1 ok (retry), action_2 ok
        assert posts == ["action_0", "action_1", "action_1", "action_2"], (
            f"Unexpected POST sequence: {posts}"
        )
        assert len(chain._offline_buffer) == 0
        assert chain._offline is False

    @pytest.mark.asyncio
    async def test_drain_breaks_on_429_then_retries(self):
        """
        event[1] raises 429 (rate limit) on the first drain pass. drain breaks
        the inner loop, sleeps (mocked to instant), then retries; all 3 land
        on the second pass.
        """
        chain = _make_chain()
        for i in range(3):
            chain._offline_buffer.append(_buffered_event(i))

        first_action1_attempt = True
        posts: list[str] = []

        async def mock_post(path, body, action_type=None):
            nonlocal first_action1_attempt
            if "events" not in path:
                return {}
            action_name = (body or {}).get("action_name", "")
            posts.append(action_name)
            if action_name == "action_1" and first_action1_attempt:
                first_action1_attempt = False
                raise _make_http_error(429, "Too many requests")
            return _ALLOW_DECISION

        with patch("arclasp.client._post", side_effect=mock_post), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            chain._drain_task = asyncio.create_task(_drain_offline_buffer(chain))
            await asyncio.wait_for(chain._drain_task, timeout=5.0)

        # Pass 1: action_0 ok, action_1 → 429 (break inner loop)
        # Pass 2: action_1 ok (retry), action_2 ok
        assert posts == ["action_0", "action_1", "action_1", "action_2"], (
            f"Unexpected POST sequence: {posts}"
        )
        assert len(chain._offline_buffer) == 0
        assert chain._offline is False
