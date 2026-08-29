from __future__ import annotations

import asyncio
from unittest.mock import patch

import httpx
import pytest

import arclasp
from arclasp.chain import Chain
from arclasp.exceptions import (
    ActionDeniedError,
    ArclaspKillSwitchError,
    ChainAutoPausedError,
    ChainCompletionError,
)


@pytest.fixture(autouse=True)
def sdk_init():
    arclasp.init(
        api_key="prail_test",
        backend_url="http://localhost:9999",
        default_approval_timeout_hours=1,
    )


async def _no_sleep(_seconds):
    return None


def _response(
    *,
    decision: str = "allow",
    policy_name: str | None = None,
    kill_switch_active: bool = False,
    auto_paused: bool = False,
):
    return {
        "policy_decision": decision,
        "decision_reason": f"{policy_name or decision} reason",
        "decision_source": "backend_evaluation",
        "policy_name": policy_name,
        "kill_switch_active": kill_switch_active,
        "auto_paused": auto_paused,
    }


def _completion_calls(calls: list[str]) -> int:
    return sum(1 for path in calls if path.endswith("/complete"))


@pytest.mark.asyncio
async def test_async_normal_success_completes_once():
    calls: list[str] = []

    async def fake_post(path, data, action_type=None, headers=None, config=None):
        calls.append(path)
        if path == "/v1/chains":
            return {"id": "chain-terminal-001"}
        if path.endswith("/complete"):
            return {"id": "chain-terminal-001", "status": "completed"}
        return _response()

    with patch("arclasp.client._post", side_effect=fake_post):
        async with Chain("normal-success") as chain:
            await chain.record_agent_action("agent", "tool_call", "get_status")

    assert _completion_calls(calls) == 1


@pytest.mark.asyncio
async def test_async_approval_approved_completes_once():
    calls: list[str] = []

    async def fake_post(path, data, action_type=None, headers=None, config=None):
        calls.append(path)
        if path == "/v1/chains":
            return {"id": "chain-terminal-001"}
        if path.endswith("/complete"):
            return {"id": "chain-terminal-001", "status": "completed"}
        return _response(decision="require_approval", policy_name="financial_approval")

    async def fake_get(path, config=None):
        assert path == "/v1/chains/chain-terminal-001/approval-status"
        return {"approval_status": "approved", "approvals": [{"decision_notes": "ok"}]}

    with (
        patch("arclasp.client._post", side_effect=fake_post),
        patch("arclasp.client._get", side_effect=fake_get),
        patch("arclasp.chain.asyncio.sleep", side_effect=_no_sleep),
    ):
        async with Chain("approval-approved") as chain:
            decision = await chain.record_agent_action(
                "agent", "tool_call", "transfer_funds", {"amount_usd": 6000}
            )

    assert decision.decision_source == "human_approval"
    assert _completion_calls(calls) == 1


@pytest.mark.asyncio
async def test_async_approval_denied_skips_completion(caplog):
    calls: list[str] = []

    async def fake_post(path, data, action_type=None, headers=None, config=None):
        calls.append(path)
        if path == "/v1/chains":
            return {"id": "chain-terminal-001"}
        if path.endswith("/complete"):
            return {"id": "chain-terminal-001", "status": "completed"}
        return _response(decision="require_approval", policy_name="financial_approval")

    async def fake_get(path, config=None):
        return {"approval_status": "denied", "approvals": [{"decision_notes": "no"}]}

    with (
        patch("arclasp.client._post", side_effect=fake_post),
        patch("arclasp.client._get", side_effect=fake_get),
        patch("arclasp.chain.asyncio.sleep", side_effect=_no_sleep),
        caplog.at_level("WARNING", logger="arclasp.chain"),
    ):
        with pytest.raises(ActionDeniedError):
            async with Chain("approval-denied") as chain:
                await chain.record_agent_action(
                    "agent", "tool_call", "transfer_funds", {"amount_usd": 6000}
                )

    assert _completion_calls(calls) == 0
    assert not any("Failed to mark chain" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_async_backend_approval_timed_out_skips_completion(caplog):
    calls: list[str] = []

    async def fake_post(path, data, action_type=None, headers=None, config=None):
        calls.append(path)
        if path == "/v1/chains":
            return {"id": "chain-terminal-001"}
        if path.endswith("/complete"):
            return {"id": "chain-terminal-001", "status": "completed"}
        return _response(decision="require_approval", policy_name="financial_approval")

    async def fake_get(path, config=None):
        return {"approval_status": "timed_out", "approvals": []}

    with (
        patch("arclasp.client._post", side_effect=fake_post),
        patch("arclasp.client._get", side_effect=fake_get),
        patch("arclasp.chain.asyncio.sleep", side_effect=_no_sleep),
        caplog.at_level("WARNING", logger="arclasp.chain"),
    ):
        with pytest.raises(ActionDeniedError) as exc_info:
            async with Chain("approval-timeout") as chain:
                await chain.record_agent_action(
                    "agent", "tool_call", "transfer_funds", {"amount_usd": 6000}
                )

    assert exc_info.value.policy_name == "approval_timeout"
    assert _completion_calls(calls) == 0
    assert not any("Failed to mark chain" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_async_kill_switch_skips_completion(caplog):
    calls: list[str] = []

    async def fake_post(path, data, action_type=None, headers=None, config=None):
        calls.append(path)
        if path == "/v1/chains":
            return {"id": "chain-terminal-001"}
        if path.endswith("/complete"):
            return {"id": "chain-terminal-001", "status": "completed"}
        return _response(
            decision="deny",
            policy_name="kill_switch",
            kill_switch_active=True,
        )

    with (
        patch("arclasp.client._post", side_effect=fake_post),
        caplog.at_level("WARNING", logger="arclasp.chain"),
    ):
        with pytest.raises(ArclaspKillSwitchError):
            async with Chain("kill-switch") as chain:
                await chain.record_agent_action("agent", "tool_call", "send_email")

    assert _completion_calls(calls) == 0
    assert not any("Failed to mark chain" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_async_generic_immediate_deny_preserves_completion_attempt():
    calls: list[str] = []

    async def fake_post(path, data, action_type=None, headers=None, config=None):
        calls.append(path)
        if path == "/v1/chains":
            return {"id": "chain-terminal-001"}
        if path.endswith("/complete"):
            return {"id": "chain-terminal-001", "status": "completed"}
        return _response(decision="deny", policy_name="credential_exposure")

    with patch("arclasp.client._post", side_effect=fake_post):
        with pytest.raises(ActionDeniedError):
            async with Chain("generic-deny") as chain:
                await chain.record_agent_action("agent", "tool_call", "log_event")

    assert _completion_calls(calls) == 1


@pytest.mark.asyncio
async def test_async_legitimate_completion_409_still_surfaces():
    calls: list[str] = []
    request = httpx.Request("POST", "http://localhost/v1/chains/chain-terminal-001/complete")
    response = httpx.Response(409, request=request)

    async def fake_post(path, data, action_type=None, headers=None, config=None):
        calls.append(path)
        if path == "/v1/chains":
            return {"id": "chain-terminal-001"}
        if path.endswith("/complete"):
            raise httpx.HTTPStatusError("conflict", request=request, response=response)
        return _response()

    with patch("arclasp.client._post", side_effect=fake_post):
        with pytest.raises(ChainCompletionError):
            async with Chain("unexpected-409") as chain:
                await chain.record_agent_action("agent", "tool_call", "get_status")

    assert _completion_calls(calls) == 1


def test_sync_kill_switch_skips_completion(caplog):
    calls: list[str] = []

    async def fake_post(path, data, action_type=None, headers=None, config=None):
        calls.append(path)
        if path == "/v1/chains":
            return {"id": "chain-terminal-001"}
        if path.endswith("/complete"):
            return {"id": "chain-terminal-001", "status": "completed"}
        return _response(
            decision="deny",
            policy_name="kill_switch",
            kill_switch_active=True,
        )

    with (
        patch("arclasp.client._post", side_effect=fake_post),
        caplog.at_level("WARNING", logger="arclasp.chain"),
    ):
        with pytest.raises(ArclaspKillSwitchError):
            with Chain("sync-kill-switch") as chain:
                asyncio.run(chain.record_agent_action("agent", "tool_call", "send_email"))

    assert _completion_calls(calls) == 0
    assert not any("Failed to mark chain" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_async_auto_pause_skips_completion(caplog):
    calls: list[str] = []

    async def fake_post(path, data, action_type=None, headers=None, config=None):
        calls.append(path)
        if path == "/v1/chains":
            return {"id": "chain-terminal-001"}
        if path.endswith("/complete"):
            return {"id": "chain-terminal-001", "status": "completed"}
        return _response(
            decision="allow",
            policy_name="auto_pause",
            auto_paused=True,
        )

    with (
        patch("arclasp.client._post", side_effect=fake_post),
        caplog.at_level("WARNING", logger="arclasp.chain"),
    ):
        with pytest.raises(ChainAutoPausedError):
            async with Chain("auto-pause") as chain:
                await chain.record_agent_action("agent", "tool_call", "loop")

    assert _completion_calls(calls) == 0
    assert not any("Failed to mark chain" in record.message for record in caplog.records)


def test_sync_auto_pause_skips_completion(caplog):
    calls: list[str] = []

    async def fake_post(path, data, action_type=None, headers=None, config=None):
        calls.append(path)
        if path == "/v1/chains":
            return {"id": "chain-terminal-001"}
        if path.endswith("/complete"):
            return {"id": "chain-terminal-001", "status": "completed"}
        return _response(
            decision="allow",
            policy_name="auto_pause",
            auto_paused=True,
        )

    with (
        patch("arclasp.client._post", side_effect=fake_post),
        caplog.at_level("WARNING", logger="arclasp.chain"),
    ):
        with pytest.raises(ChainAutoPausedError):
            with Chain("sync-auto-pause") as chain:
                asyncio.run(chain.record_agent_action("agent", "tool_call", "loop"))

    assert _completion_calls(calls) == 0
    assert not any("Failed to mark chain" in record.message for record in caplog.records)


def test_sync_approval_denied_skips_completion(caplog):
    calls: list[str] = []

    async def fake_post(path, data, action_type=None, headers=None, config=None):
        calls.append(path)
        if path == "/v1/chains":
            return {"id": "chain-terminal-001"}
        if path.endswith("/complete"):
            return {"id": "chain-terminal-001", "status": "completed"}
        return _response(decision="require_approval", policy_name="financial_approval")

    async def fake_get(path, config=None):
        return {"approval_status": "denied", "approvals": [{"decision_notes": "no"}]}

    with (
        patch("arclasp.client._post", side_effect=fake_post),
        patch("arclasp.client._get", side_effect=fake_get),
        patch("arclasp.chain.asyncio.sleep", side_effect=_no_sleep),
        caplog.at_level("WARNING", logger="arclasp.chain"),
    ):
        with pytest.raises(ActionDeniedError):
            with Chain("sync-approval-denied") as chain:
                asyncio.run(
                    chain.record_agent_action(
                        "agent", "tool_call", "transfer_funds", {"amount_usd": 6000}
                    )
                )

    assert _completion_calls(calls) == 0
    assert not any("Failed to mark chain" in record.message for record in caplog.records)


def test_sync_backend_approval_timed_out_skips_completion(caplog):
    calls: list[str] = []

    async def fake_post(path, data, action_type=None, headers=None, config=None):
        calls.append(path)
        if path == "/v1/chains":
            return {"id": "chain-terminal-001"}
        if path.endswith("/complete"):
            return {"id": "chain-terminal-001", "status": "completed"}
        return _response(decision="require_approval", policy_name="financial_approval")

    async def fake_get(path, config=None):
        return {"approval_status": "timed_out", "approvals": []}

    with (
        patch("arclasp.client._post", side_effect=fake_post),
        patch("arclasp.client._get", side_effect=fake_get),
        patch("arclasp.chain.asyncio.sleep", side_effect=_no_sleep),
        caplog.at_level("WARNING", logger="arclasp.chain"),
    ):
        with pytest.raises(ActionDeniedError) as exc_info:
            with Chain("sync-approval-timeout") as chain:
                asyncio.run(
                    chain.record_agent_action(
                        "agent", "tool_call", "transfer_funds", {"amount_usd": 6000}
                    )
                )

    assert exc_info.value.policy_name == "approval_timeout"
    assert _completion_calls(calls) == 0
    assert not any("Failed to mark chain" in record.message for record in caplog.records)


def test_sync_generic_immediate_deny_preserves_completion_attempt():
    calls: list[str] = []

    async def fake_post(path, data, action_type=None, headers=None, config=None):
        calls.append(path)
        if path == "/v1/chains":
            return {"id": "chain-terminal-001"}
        if path.endswith("/complete"):
            return {"id": "chain-terminal-001", "status": "completed"}
        return _response(decision="deny", policy_name="credential_exposure")

    with patch("arclasp.client._post", side_effect=fake_post):
        with pytest.raises(ActionDeniedError):
            with Chain("sync-generic-deny") as chain:
                asyncio.run(chain.record_agent_action("agent", "tool_call", "log_event"))

    assert _completion_calls(calls) == 1
