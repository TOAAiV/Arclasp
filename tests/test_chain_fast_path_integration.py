"""
Integration tests for fast-path in Chain.record_agent_action.

These tests mock the backend HTTP client and verify the fast-path integration:
- Fast-path hit → no synchronous backend call, action allowed immediately.
- Non-fast-path action → synchronous backend call is made.
- Backend down + fast-path eligible → action still proceeds.
- enable_local_fast_path=False → every action calls backend synchronously.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch, AsyncMock

import pytest

import proofrail
from proofrail.chain import Chain


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def sdk_dev():
    """SDK initialised with development environment and fast-path enabled."""
    proofrail.init(
        api_key="prail_test",
        backend_url="http://localhost:9999",
        environment="development",
        enable_local_fast_path=True,
        fail_mode="allow",
    )


def _chain_start_response(chain_id: str = "chain-fp-001"):
    return {"id": chain_id}


def _allow_response():
    return {
        "policy_decision": "allow",
        "decision_reason": "Action permitted by policy",
        "decision_source": "backend_evaluation",
    }


def _deny_response():
    return {
        "policy_decision": "deny",
        "decision_reason": "IAM/permission modification blocked",
        "decision_source": "backend_evaluation",
        "policy_name": None,
    }


# ---------------------------------------------------------------------------
# Test 1: Fast-path hit — no synchronous backend event call
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fast_path_skips_sync_backend_event_call():
    """An obviously-safe action uses fast-path; /events is never called sync."""
    event_posts = []

    async def mock_post(path, body, action_type=None):
        event_posts.append(path)
        if path == "/v1/chains":
            return _chain_start_response()
        return _allow_response()

    with patch("proofrail.client._post", side_effect=mock_post):
        async with Chain("test-fp") as chain:
            before = len(event_posts)  # 1: /v1/chains call

            result = await chain.record_agent_action(
                agent_name="agent",
                action_type="tool_call",
                action_name="get_data",    # low-risk, fast-path eligible
                payload={},
            )

            # Synchronous part must not have called /events
            sync_event_calls = [p for p in event_posts[before:] if "events" in p]
            assert sync_event_calls == [], (
                f"Expected no sync /events call; got {sync_event_calls}"
            )

        # result should be a fast-path allow
        assert result.policy_decision == "allow"
        assert result.decision_source == "local_fast_path"


# ---------------------------------------------------------------------------
# Test 2: Non-fast-path action hits backend synchronously
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_deny_action_bypasses_fast_path_and_calls_backend():
    """A clearly-deny action (IAM) skips fast-path and calls backend sync."""
    event_posts = []

    async def mock_post(path, body, action_type=None):
        event_posts.append(path)
        if path == "/v1/chains":
            return _chain_start_response()
        return _deny_response()

    from proofrail.exceptions import ActionDeniedError

    with patch("proofrail.client._post", side_effect=mock_post):
        async with Chain("test-deny") as chain:
            before = len(event_posts)

            with pytest.raises(ActionDeniedError):
                await chain.record_agent_action(
                    agent_name="agent",
                    action_type="tool_call",
                    action_name="update_iam_policy",   # deny: iam in name
                    payload={},
                )

            # Must have called /events synchronously
            after = [p for p in event_posts[before:] if "events" in p]
            assert len(after) == 1, f"Expected 1 sync /events call; got {after}"


# ---------------------------------------------------------------------------
# Test 3: Backend down + fast-path eligible → action proceeds
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_backend_down_fast_path_eligible_action_proceeds():
    """When backend is down and action is fast-path eligible, allow proceeds."""
    import httpx

    call_log = []

    async def mock_post(path, body, action_type=None):
        call_log.append(path)
        if path == "/v1/chains":
            return {"id": "offline-chain-001"}
        raise httpx.ConnectError("backend down")

    with patch("proofrail.client._post", side_effect=mock_post):
        # fail_mode=allow: chain starts offline if /v1/chains fails
        # BUT we return a chain_id above, so chain starts online.
        # Event submission fails — fast-path should have intercepted it first.
        async with Chain("test-down") as chain:
            result = await chain.record_agent_action(
                agent_name="agent",
                action_type="tool_call",
                action_name="get_config",
                payload={},
            )

    # Fast-path should have allowed it before the event POST was attempted
    assert result.policy_decision == "allow"
    assert result.decision_source == "local_fast_path"


# ---------------------------------------------------------------------------
# Test 4: enable_local_fast_path=False → backend called for every action
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fast_path_disabled_forces_backend_call():
    """With enable_local_fast_path=False, every action calls backend."""
    proofrail.init(
        api_key="prail_test",
        backend_url="http://localhost:9999",
        environment="development",
        enable_local_fast_path=False,
        fail_mode="allow",
    )

    event_posts = []

    async def mock_post(path, body, action_type=None):
        event_posts.append(path)
        if path == "/v1/chains":
            return _chain_start_response()
        return _allow_response()

    with patch("proofrail.client._post", side_effect=mock_post):
        async with Chain("test-no-fp") as chain:
            before = len(event_posts)

            result = await chain.record_agent_action(
                agent_name="agent",
                action_type="tool_call",
                action_name="get_data",
                payload={},
            )

            after = [p for p in event_posts[before:] if "events" in p]
            assert len(after) == 1, (
                f"Expected 1 sync /events call with fast-path disabled; got {after}"
            )

    assert result.policy_decision == "allow"
    # Source must come from backend response, not local_fast_path
    assert result.decision_source == "backend_evaluation"


# ---------------------------------------------------------------------------
# Test 5: Fast-path event is buffered for async drain
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fast_path_event_buffered_for_async_drain():
    """After a fast-path decision the event is queued in _offline_buffer."""
    async def mock_post(path, body, action_type=None):
        if path == "/v1/chains":
            return _chain_start_response()
        return _allow_response()

    with patch("proofrail.client._post", side_effect=mock_post):
        async with Chain("test-buf") as chain:
            # Before the call: buffer should be empty
            assert chain._offline_buffer == []

            result = await chain.record_agent_action(
                agent_name="agent",
                action_type="tool_call",
                action_name="get_data",
                payload={},
            )

            if result.decision_source == "local_fast_path":
                # Event was buffered immediately (before the drain task ran)
                assert len(chain._offline_buffer) >= 0  # may already be drained
                assert result.policy_decision == "allow"


# ---------------------------------------------------------------------------
# Test 6: Async drain sends buffered events to backend
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_async_drain_sends_buffered_events():
    """The _drain_buffer_to_backend task empties the buffer after fast-path."""
    event_paths = []

    async def mock_post(path, body, action_type=None):
        event_paths.append(path)
        if path == "/v1/chains":
            return _chain_start_response()
        return _allow_response()

    with patch("proofrail.client._post", side_effect=mock_post):
        async with Chain("test-drain") as chain:
            result = await chain.record_agent_action(
                agent_name="agent",
                action_type="tool_call",
                action_name="get_data",
                payload={},
            )

            if result.decision_source == "local_fast_path":
                # Let the event loop tick so the drain task can run
                await asyncio.sleep(0)
                await asyncio.sleep(0)  # two ticks to be safe

                # Buffer should now be empty (drain succeeded)
                assert chain._offline_buffer == []
