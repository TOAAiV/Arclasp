from __future__ import annotations

import asyncio
import logging
from unittest.mock import patch

import pytest

import arclasp
from arclasp.chain import Chain
from arclasp.exceptions import ChainCompletionError


@pytest.fixture(autouse=True)
def sdk_init():
    arclasp.init(api_key="prail_test", backend_url="http://localhost:9999")


def _post_factory(*complete_results):
    results = list(complete_results)
    calls: list[str] = []

    async def fake_post(path, data, action_type=None, headers=None):
        calls.append(path)
        if path == "/v1/chains":
            return {"id": "chain-complete-001"}
        result = results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    return fake_post, calls


@pytest.mark.asyncio
async def test_async_context_successful_body_successful_completion():
    fake_post, calls = _post_factory({"id": "chain-complete-001", "status": "completed"})
    with patch("arclasp.client._post", side_effect=fake_post):
        async with Chain("completion-succeeds"):
            pass

    assert calls == ["/v1/chains", "/v1/chains/chain-complete-001/complete"]


@pytest.mark.asyncio
async def test_async_context_body_cancelled_completion_succeeds_then_cancellation_propagates():
    fake_post, calls = _post_factory({"id": "chain-complete-001", "status": "completed"})
    with patch("arclasp.client._post", side_effect=fake_post):
        with pytest.raises(asyncio.CancelledError):
            async with Chain("body-cancelled"):
                raise asyncio.CancelledError

    assert calls == ["/v1/chains", "/v1/chains/chain-complete-001/complete"]


@pytest.mark.asyncio
async def test_async_context_cancellation_during_completion_waits_for_completion_once():
    complete_entered = asyncio.Event()
    release_complete = asyncio.Event()
    calls: list[str] = []

    async def fake_post(path, data, action_type=None, headers=None):
        calls.append(path)
        if path == "/v1/chains":
            return {"id": "chain-complete-001"}
        complete_entered.set()
        await release_complete.wait()
        return {"id": "chain-complete-001", "status": "completed"}

    async def run_chain():
        with patch("arclasp.client._post", side_effect=fake_post):
            async with Chain("cancel-during-complete"):
                pass

    task = asyncio.create_task(run_chain())
    await complete_entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()

    release_complete.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls == ["/v1/chains", "/v1/chains/chain-complete-001/complete"]


@pytest.mark.asyncio
async def test_async_context_repeated_cancellation_during_completion_uses_one_completion_task():
    complete_entered = asyncio.Event()
    release_complete = asyncio.Event()
    calls: list[str] = []

    async def fake_post(path, data, action_type=None, headers=None):
        calls.append(path)
        if path == "/v1/chains":
            return {"id": "chain-complete-001"}
        complete_entered.set()
        await release_complete.wait()
        return {"id": "chain-complete-001", "status": "completed"}

    async def run_chain():
        with patch("arclasp.client._post", side_effect=fake_post):
            async with Chain("repeated-cancel"):
                pass

    task = asyncio.create_task(run_chain())
    await complete_entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()

    release_complete.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls.count("/v1/chains/chain-complete-001/complete") == 1


@pytest.mark.asyncio
async def test_async_context_cancellation_keeps_cancel_primary_when_completion_fails(caplog):
    complete_entered = asyncio.Event()
    release_complete = asyncio.Event()
    calls: list[str] = []

    async def fake_post(path, data, action_type=None, headers=None):
        calls.append(path)
        if path == "/v1/chains":
            return {"id": "chain-complete-001"}
        complete_entered.set()
        await release_complete.wait()
        raise RuntimeError("backend unavailable")

    async def run_chain():
        with patch("arclasp.client._post", side_effect=fake_post):
            async with Chain("cancel-and-complete-fails"):
                pass

    task = asyncio.create_task(run_chain())
    await complete_entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    release_complete.set()

    with caplog.at_level(logging.WARNING, logger="arclasp.chain"):
        with pytest.raises(asyncio.CancelledError):
            await task

    assert calls == ["/v1/chains", "/v1/chains/chain-complete-001/complete"]
    assert any(
        "Chain completion failed after cancellation" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_async_context_successful_body_completion_failure_is_observable():
    fake_post, calls = _post_factory(RuntimeError("backend unavailable"))
    with patch("arclasp.client._post", side_effect=fake_post):
        with pytest.raises(ChainCompletionError) as exc_info:
            async with Chain("completion-fails"):
                pass

    assert exc_info.value.chain_id == "chain-complete-001"
    assert calls == ["/v1/chains", "/v1/chains/chain-complete-001/complete"]


@pytest.mark.asyncio
async def test_async_context_user_exception_takes_precedence_over_completion_failure():
    fake_post, _calls = _post_factory(RuntimeError("backend unavailable"))
    with patch("arclasp.client._post", side_effect=fake_post):
        with pytest.raises(ValueError, match="user code failed"):
            async with Chain("body-fails"):
                raise ValueError("user code failed")


@pytest.mark.asyncio
async def test_async_context_user_exception_and_external_cancellation_prefers_cancellation():
    complete_entered = asyncio.Event()
    release_complete = asyncio.Event()
    calls: list[str] = []

    async def fake_post(path, data, action_type=None, headers=None):
        calls.append(path)
        if path == "/v1/chains":
            return {"id": "chain-complete-001"}
        complete_entered.set()
        await release_complete.wait()
        return {"id": "chain-complete-001", "status": "completed"}

    async def run_chain():
        with patch("arclasp.client._post", side_effect=fake_post):
            async with Chain("user-error-and-cancel"):
                raise ValueError("user code failed")

    task = asyncio.create_task(run_chain())
    await complete_entered.wait()
    task.cancel()
    release_complete.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls == ["/v1/chains", "/v1/chains/chain-complete-001/complete"]


@pytest.mark.asyncio
async def test_async_context_no_orphan_completion_task_after_cancellation():
    complete_entered = asyncio.Event()
    release_complete = asyncio.Event()
    created_completion_tasks: list[asyncio.Task] = []
    real_create_task = asyncio.create_task

    async def fake_post(path, data, action_type=None, headers=None):
        if path == "/v1/chains":
            return {"id": "chain-complete-001"}
        complete_entered.set()
        await release_complete.wait()
        return {"id": "chain-complete-001", "status": "completed"}

    def spy_create_task(coro, *args, **kwargs):
        task = real_create_task(coro, *args, **kwargs)
        created_completion_tasks.append(task)
        return task

    async def run_chain():
        with patch("arclasp.client._post", side_effect=fake_post):
            with patch("arclasp.chain.asyncio.create_task", side_effect=spy_create_task):
                async with Chain("no-orphan-completion"):
                    pass

    task = asyncio.create_task(run_chain())
    await complete_entered.wait()
    task.cancel()
    release_complete.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(created_completion_tasks) == 1
    assert all(completion_task.done() for completion_task in created_completion_tasks)


@pytest.mark.asyncio
async def test_async_context_cancellation_during_event_recording_still_attempts_completion_once():
    event_entered = asyncio.Event()
    release_event = asyncio.Event()
    calls: list[str] = []

    async def fake_post(path, data, action_type=None, headers=None):
        calls.append(path)
        if path == "/v1/chains":
            return {"id": "chain-complete-001"}
        if path.endswith("/events"):
            event_entered.set()
            await release_event.wait()
            return {
                "policy_decision": "allow",
                "decision_reason": "",
                "decision_source": "backend_evaluation",
            }
        return {"id": "chain-complete-001", "status": "completed"}

    async def run_chain():
        with patch("arclasp.client._post", side_effect=fake_post):
            async with Chain("cancel-during-event") as chain:
                await chain.record_agent_action(
                    agent_name="agent",
                    action_type="tool_call",
                    action_name="blocked",
                    payload={},
                )

    task = asyncio.create_task(run_chain())
    await event_entered.wait()
    task.cancel()
    release_event.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls.count("/v1/chains/chain-complete-001/events") == 1
    assert calls.count("/v1/chains/chain-complete-001/complete") == 1


@pytest.mark.asyncio
async def test_explicit_completion_can_retry_after_response_loss():
    fake_post, calls = _post_factory(
        RuntimeError("response lost"),
        {"id": "chain-complete-001", "status": "completed"},
    )
    chain = Chain("retry")
    with patch("arclasp.client._post", side_effect=fake_post):
        await chain._start()
        with pytest.raises(ChainCompletionError):
            await chain._complete()
        await chain._complete()

    assert calls == [
        "/v1/chains",
        "/v1/chains/chain-complete-001/complete",
        "/v1/chains/chain-complete-001/complete",
    ]


@pytest.mark.asyncio
async def test_explicit_completion_called_twice_is_backend_idempotent():
    fake_post, calls = _post_factory(
        {"id": "chain-complete-001", "status": "completed"},
        {"id": "chain-complete-001", "status": "completed"},
    )
    chain = Chain("double-complete")
    with patch("arclasp.client._post", side_effect=fake_post):
        await chain._start()
        await chain._complete()
        await chain._complete()

    assert calls.count("/v1/chains/chain-complete-001/complete") == 2


@pytest.mark.asyncio
async def test_completion_malformed_response_is_observable():
    fake_post, _calls = _post_factory({"id": "chain-complete-001", "status": "active"})
    chain = Chain("malformed-complete")
    with patch("arclasp.client._post", side_effect=fake_post):
        await chain._start()
        with pytest.raises(ChainCompletionError):
            await chain._complete()


def test_sync_context_successful_body_completion_failure_is_observable():
    fake_post, calls = _post_factory(RuntimeError("backend unavailable"))
    with patch("arclasp.client._post", side_effect=fake_post):
        with pytest.raises(ChainCompletionError):
            with Chain("sync-completion-fails"):
                pass

    assert calls == ["/v1/chains", "/v1/chains/chain-complete-001/complete"]


def test_sync_context_user_exception_takes_precedence_over_completion_failure():
    fake_post, _calls = _post_factory(RuntimeError("backend unavailable"))
    with patch("arclasp.client._post", side_effect=fake_post):
        with pytest.raises(ValueError, match="user code failed"):
            with Chain("sync-body-fails"):
                raise ValueError("user code failed")
