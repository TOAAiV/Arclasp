"""
Regression tests for BUG-LR-01 drain timeout formula on transitional buffer state.

Public K1 governed execution no longer creates fast-path/offline buffers, but the
legacy drain fields remain in Chain for compatibility and should still close
predictably if pre-existing internal state is present.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import patch

import pytest

import proofrail
from proofrail.chain import Chain


def _init(backend_timeout_seconds: int = 2, drain_timeout_seconds: int | None = None) -> None:
    kw = dict(
        api_key="prail_test",
        backend_url="http://test.invalid",
        environment="development",
        enable_local_fast_path=False,
        fail_mode="deny",
        backend_timeout_seconds=backend_timeout_seconds,
    )
    if drain_timeout_seconds is not None:
        kw["drain_timeout_seconds"] = drain_timeout_seconds
    proofrail.init(**kw)


async def _never_finishes() -> None:
    await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_drain_timeout_scales_with_buffer_size():
    _init(backend_timeout_seconds=2)
    chain = Chain("lr01-scale-test")
    chain._chain_id = "test-chain-id"
    chain._offline_buffer = [{"action_name": f"action_{i}"} for i in range(5)]
    chain._drain_task = asyncio.create_task(_never_finishes())

    wait_for_calls: list[float] = []

    async def spy_wait_for(coro, timeout=None, **kw):
        wait_for_calls.append(timeout)
        raise asyncio.TimeoutError

    async def complete_post(path: str, body: dict, action_type: str | None = None) -> dict:
        return {"status": "completed"}

    with patch("proofrail.chain.asyncio.wait_for", side_effect=spy_wait_for):
        with patch("proofrail.client._post", side_effect=complete_post):
            await chain._complete()

    assert wait_for_calls == [15.0]
    chain._drain_task.cancel()


@pytest.mark.asyncio
async def test_drain_timeout_drop_count_logged(caplog):
    _init(backend_timeout_seconds=1)
    chain = Chain("lr01-drop-count-test")
    chain._chain_id = "test-chain-id"
    chain._offline_buffer = [{"action_name": f"action_{i}"} for i in range(3)]
    chain._drain_task = asyncio.create_task(_never_finishes())

    async def spy_wait_for(coro, timeout=None, **kw):
        raise asyncio.TimeoutError

    async def complete_post(path: str, body: dict, action_type: str | None = None) -> dict:
        return {"status": "completed"}

    with caplog.at_level(logging.WARNING, logger="proofrail.chain"):
        with patch("proofrail.chain.asyncio.wait_for", side_effect=spy_wait_for):
            with patch("proofrail.client._post", side_effect=complete_post):
                await chain._complete()

    assert any("3 event(s) dropped" in r.message for r in caplog.records)
    assert any("timeout=10.0s" in r.message for r in caplog.records)
    chain._drain_task.cancel()


@pytest.mark.asyncio
async def test_drain_timeout_config_override():
    _init(backend_timeout_seconds=2, drain_timeout_seconds=60)
    chain = Chain("lr01-override-test")
    chain._chain_id = "test-chain-id"
    chain._offline_buffer = [{"action_name": f"action_{i}"} for i in range(3)]
    chain._drain_task = asyncio.create_task(_never_finishes())

    wait_for_calls: list[float] = []

    async def spy_wait_for(coro, timeout=None, **kw):
        wait_for_calls.append(timeout)
        raise asyncio.TimeoutError

    async def complete_post(path: str, body: dict, action_type: str | None = None) -> dict:
        return {"status": "completed"}

    with patch("proofrail.chain.asyncio.wait_for", side_effect=spy_wait_for):
        with patch("proofrail.client._post", side_effect=complete_post):
            await chain._complete()

    assert wait_for_calls == [60.0]
    chain._drain_task.cancel()
