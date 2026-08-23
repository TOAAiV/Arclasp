from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

import arclasp
from arclasp import client as _client
from arclasp.chain import Chain
from arclasp.exceptions import BackendUnavailableError


@dataclass
class RecordedRequest:
    base_url: str
    authorization: str
    timeout: Any
    path: str
    body: dict
    headers: dict | None


class _FakeAsyncClient:
    records: list[RecordedRequest] = []
    close_count = 0
    fail_once_paths: set[tuple[str, str]] = set()
    attempts: dict[tuple[str, str], int] = {}

    def __init__(self, *, base_url, timeout, headers):
        self.base_url = str(base_url)
        self.timeout = timeout
        self.headers = dict(headers)

    async def aclose(self) -> None:
        type(self).close_count += 1

    async def post(self, path, json=None, headers=None):
        key = (self.headers["Authorization"], path)
        attempts = type(self).attempts.get(key, 0) + 1
        type(self).attempts[key] = attempts
        if key in type(self).fail_once_paths and attempts == 1:
            raise httpx.TimeoutException("simulated timeout")

        body = dict(json or {})
        type(self).records.append(
            RecordedRequest(
                base_url=self.base_url,
                authorization=self.headers["Authorization"],
                timeout=self.timeout,
                path=path,
                body=body,
                headers=dict(headers) if headers else None,
            )
        )
        return httpx.Response(
            200,
            json=_response_for(path, self.headers["Authorization"]),
            request=httpx.Request("POST", f"{self.base_url}{path}"),
        )

    async def get(self, path):
        type(self).records.append(
            RecordedRequest(
                base_url=self.base_url,
                authorization=self.headers["Authorization"],
                timeout=self.timeout,
                path=path,
                body={},
                headers=None,
            )
        )
        return httpx.Response(
            200,
            json={"approval_status": "approved", "approvals": []},
            request=httpx.Request("GET", f"{self.base_url}{path}"),
        )


def _response_for(path: str, authorization: str) -> dict:
    suffix = authorization.rsplit("_", 1)[-1].lower()
    if path == "/v1/chains":
        return {"id": f"chain-{suffix}"}
    if path.endswith("/events"):
        return {"policy_decision": "allow", "decision_source": "backend_evaluation"}
    if path.endswith("/complete"):
        return {"status": "completed"}
    return {"ok": True}


@pytest.fixture(autouse=True)
def reset_sdk():
    _client._reset_for_tests()
    _FakeAsyncClient.records = []
    _FakeAsyncClient.close_count = 0
    _FakeAsyncClient.fail_once_paths = set()
    _FakeAsyncClient.attempts = {}
    yield
    _client._reset_for_tests()


@pytest.fixture
def fake_http_client(monkeypatch):
    monkeypatch.setattr(_client.httpx, "AsyncClient", _FakeAsyncClient)
    return _FakeAsyncClient


async def _run_chain(label: str) -> None:
    async with Chain(label) as chain:
        await chain.record_agent_action(
            agent_name=f"agent-{label}",
            action_type="tool_call",
            action_name=f"action-{label}",
        )


def _auths_for_chain(label: str) -> list[str]:
    chain_id = f"chain-{label.lower()}"
    return [
        r.authorization
        for r in _FakeAsyncClient.records
        if (
            r.path == "/v1/chains"
            and r.body.get("external_chain_id") == label
        )
        or r.path.startswith(f"/v1/chains/{chain_id}/")
    ]


@pytest.mark.asyncio
async def test_chain_snapshots_init_config_across_reinit(fake_http_client):
    arclasp.init(api_key="prail_A", backend_url="http://backend-a")
    chain_a = Chain("A")
    arclasp.init(api_key="prail_B", backend_url="http://backend-b")
    chain_b = Chain("B")

    async with chain_a:
        await chain_a.record_agent_action("agent-a", "tool_call", "event-a")
    async with chain_b:
        await chain_b.record_agent_action("agent-b", "tool_call", "event-b")

    assert set(_auths_for_chain("A")) == {"Bearer prail_A"}
    assert set(_auths_for_chain("B")) == {"Bearer prail_B"}
    assert {r.base_url for r in _FakeAsyncClient.records if "chain-a" in r.path} == {
        "http://backend-a"
    }
    assert {r.base_url for r in _FakeAsyncClient.records if "chain-b" in r.path} == {
        "http://backend-b"
    }


@pytest.mark.asyncio
async def test_chain_created_before_init_binds_once_on_first_use(fake_http_client):
    chain = Chain("A")
    arclasp.init(api_key="prail_A", backend_url="http://backend-a")
    async with chain:
        pass
    arclasp.init(api_key="prail_B", backend_url="http://backend-b")
    await chain._complete()

    assert set(_auths_for_chain("A")) == {"Bearer prail_A"}


@pytest.mark.asyncio
async def test_async_tasks_keep_context_local_configs(fake_http_client):
    mixed = 0

    async def worker(label: str, ready: asyncio.Event, go: asyncio.Event) -> None:
        arclasp.init(api_key=f"prail_{label}", backend_url=f"http://backend-{label}")
        ready.set()
        await go.wait()
        await _run_chain(label)

    for i in range(100):
        ready_a = asyncio.Event()
        ready_b = asyncio.Event()
        go = asyncio.Event()
        task_a = asyncio.create_task(worker(f"A{i}", ready_a, go))
        task_b = asyncio.create_task(worker(f"B{i}", ready_b, go))
        await ready_a.wait()
        await ready_b.wait()
        go.set()
        await asyncio.gather(task_a, task_b)

    for record in _FakeAsyncClient.records:
        if "/chain-a" in record.path.lower() and not record.authorization.startswith(
            "Bearer prail_A"
        ):
            mixed += 1
        if "/chain-b" in record.path.lower() and not record.authorization.startswith(
            "Bearer prail_B"
        ):
            mixed += 1
    assert mixed == 0


@pytest.mark.asyncio
async def test_child_tasks_inherit_parent_config(fake_http_client):
    arclasp.init(api_key="prail_PARENT", backend_url="http://backend-parent")
    await asyncio.gather(_run_chain("P1"), _run_chain("P2"))

    assert {r.authorization for r in _FakeAsyncClient.records} == {
        "Bearer prail_PARENT"
    }


def test_threads_keep_independent_configs(fake_http_client):
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker(label: str) -> None:
        try:
            arclasp.init(api_key=f"prail_{label}", backend_url=f"http://backend-{label}")
            barrier.wait(timeout=5)
            with Chain(label):
                pass
        except BaseException as exc:
            errors.append(exc)

    thread_a = threading.Thread(target=worker, args=("A",))
    thread_b = threading.Thread(target=worker, args=("B",))
    thread_a.start()
    thread_b.start()
    thread_a.join()
    thread_b.join()

    assert errors == []
    assert set(_auths_for_chain("A")) == {"Bearer prail_A"}
    assert set(_auths_for_chain("B")) == {"Bearer prail_B"}


@pytest.mark.asyncio
async def test_one_loop_cache_distinguishes_credentials_backend_and_timeout(fake_http_client):
    arclasp.init(
        api_key="prail_A",
        backend_url="http://backend-a",
        backend_timeout_seconds=1,
    )
    chain_a = Chain("A")
    arclasp.init(
        api_key="prail_B",
        backend_url="http://backend-b",
        backend_timeout_seconds=9,
    )
    chain_b = Chain("B")

    await chain_a._start()
    await chain_b._start()

    by_auth = {r.authorization: r for r in _FakeAsyncClient.records}
    assert by_auth["Bearer prail_A"].base_url == "http://backend-a"
    assert by_auth["Bearer prail_B"].base_url == "http://backend-b"
    assert by_auth["Bearer prail_A"].timeout.connect == 1
    assert by_auth["Bearer prail_B"].timeout.connect == 9


@pytest.mark.asyncio
async def test_retry_configuration_is_bound_per_request(fake_http_client):
    arclasp.init(
        api_key="prail_A",
        backend_url="http://backend-a",
        max_retries=0,
        retry_backoff_base_ms=0,
    )
    config_a = _client._snapshot_config()
    arclasp.init(
        api_key="prail_B",
        backend_url="http://backend-b",
        max_retries=1,
        retry_backoff_base_ms=0,
    )
    config_b = _client._snapshot_config()
    _FakeAsyncClient.fail_once_paths = {
        ("Bearer prail_A", "/v1/retry"),
        ("Bearer prail_B", "/v1/retry"),
    }

    with pytest.raises(BackendUnavailableError):
        await _client._post("/v1/retry", {}, config=config_a)
    await _client._post("/v1/retry", {}, config=config_b)

    assert _FakeAsyncClient.attempts[("Bearer prail_A", "/v1/retry")] == 1
    assert _FakeAsyncClient.attempts[("Bearer prail_B", "/v1/retry")] == 2


@pytest.mark.asyncio
async def test_reinit_preserves_previous_context_client(fake_http_client):
    arclasp.init(api_key="prail_A", backend_url="http://backend-a")
    old_client = _client._get_client()
    arclasp.init(api_key="prail_B", backend_url="http://backend-b")
    await asyncio.sleep(0)

    assert _FakeAsyncClient.close_count == 0
    assert old_client.headers["Authorization"] == "Bearer prail_A"
    assert _client._get_client().headers["Authorization"] == "Bearer prail_B"


@pytest.mark.asyncio
async def test_get_client_recreates_closed_cached_client(fake_http_client):
    arclasp.init(api_key="prail_A", backend_url="http://backend-a")
    config = _client._snapshot_config()
    old_client = _client._get_client(config)
    old_client.is_closed = True

    new_client = _client._get_client(config)

    assert new_client is not old_client
    assert new_client.headers["Authorization"] == "Bearer prail_A"


def test_public_api_surface_unchanged():
    assert arclasp.__all__ == [
        "init",
        "verify_approval_v2",
        "verify_public_token",
        "verify_receipt_v2",
        "Chain",
        "ArclaspPolicyError",
        "ActionDeniedError",
        "BackendUnavailableError",
        "ChainCompletionError",
        "ChainAutoPausedError",
        "ChainTimeoutError",
        "ArclaspKillSwitchError",
        "ArclaspVerificationError",
        "__version__",
    ]
    assert not hasattr(arclasp, "ArclaspClient")
