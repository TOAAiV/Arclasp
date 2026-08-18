from __future__ import annotations

from unittest.mock import patch

import pytest

import proofrail
from proofrail.chain import Chain
from proofrail.exceptions import ChainCompletionError


@pytest.fixture(autouse=True)
def sdk_init():
    proofrail.init(api_key="prail_test", backend_url="http://localhost:9999")


def _post_factory(*complete_results):
    results = list(complete_results)
    calls: list[str] = []

    async def fake_post(path, data, action_type=None):
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
    with patch("proofrail.client._post", side_effect=fake_post):
        async with Chain("completion-succeeds"):
            pass

    assert calls == ["/v1/chains", "/v1/chains/chain-complete-001/complete"]


@pytest.mark.asyncio
async def test_async_context_successful_body_completion_failure_is_observable():
    fake_post, calls = _post_factory(RuntimeError("backend unavailable"))
    with patch("proofrail.client._post", side_effect=fake_post):
        with pytest.raises(ChainCompletionError) as exc_info:
            async with Chain("completion-fails"):
                pass

    assert exc_info.value.chain_id == "chain-complete-001"
    assert calls == ["/v1/chains", "/v1/chains/chain-complete-001/complete"]


@pytest.mark.asyncio
async def test_async_context_user_exception_takes_precedence_over_completion_failure():
    fake_post, _calls = _post_factory(RuntimeError("backend unavailable"))
    with patch("proofrail.client._post", side_effect=fake_post):
        with pytest.raises(ValueError, match="user code failed"):
            async with Chain("body-fails"):
                raise ValueError("user code failed")


@pytest.mark.asyncio
async def test_explicit_completion_can_retry_after_response_loss():
    fake_post, calls = _post_factory(
        RuntimeError("response lost"),
        {"id": "chain-complete-001", "status": "completed"},
    )
    chain = Chain("retry")
    with patch("proofrail.client._post", side_effect=fake_post):
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
    with patch("proofrail.client._post", side_effect=fake_post):
        await chain._start()
        await chain._complete()
        await chain._complete()

    assert calls.count("/v1/chains/chain-complete-001/complete") == 2


@pytest.mark.asyncio
async def test_completion_malformed_response_is_observable():
    fake_post, _calls = _post_factory({"id": "chain-complete-001", "status": "active"})
    chain = Chain("malformed-complete")
    with patch("proofrail.client._post", side_effect=fake_post):
        await chain._start()
        with pytest.raises(ChainCompletionError):
            await chain._complete()


def test_sync_context_successful_body_completion_failure_is_observable():
    fake_post, calls = _post_factory(RuntimeError("backend unavailable"))
    with patch("proofrail.client._post", side_effect=fake_post):
        with pytest.raises(ChainCompletionError):
            with Chain("sync-completion-fails"):
                pass

    assert calls == ["/v1/chains", "/v1/chains/chain-complete-001/complete"]


def test_sync_context_user_exception_takes_precedence_over_completion_failure():
    fake_post, _calls = _post_factory(RuntimeError("backend unavailable"))
    with patch("proofrail.client._post", side_effect=fake_post):
        with pytest.raises(ValueError, match="user code failed"):
            with Chain("sync-body-fails"):
                raise ValueError("user code failed")
