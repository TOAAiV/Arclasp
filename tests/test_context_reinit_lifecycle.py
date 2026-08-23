from __future__ import annotations

import asyncio
import contextlib
import json
import warnings
from dataclasses import dataclass, field
from typing import Any

import pytest
import pytest_asyncio

import arclasp
from arclasp import client as _client
from arclasp.chain import Chain


@dataclass
class RecordedRequest:
    path: str
    authorization: str
    body: dict[str, Any]


@dataclass
class LocalBackend:
    server: asyncio.AbstractServer
    base_url: str
    records: list[RecordedRequest] = field(default_factory=list)
    wrong_credentials: int = 0
    lifecycle_errors: int = 0
    block_paths: set[str] = field(default_factory=set)
    blocked: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def close(self) -> None:
        self.server.close()
        await self.server.wait_closed()

    def auths_for(self, external_chain_id: str) -> list[str]:
        chain_id = f"chain-{external_chain_id}"
        return [
            record.authorization
            for record in self.records
            if record.body.get("external_chain_id") == external_chain_id
            or record.path.startswith(f"/v1/chains/{chain_id}/")
        ]


async def _close_cached_clients() -> None:
    clients = [
        client
        for clients_for_loop in list(_client._clients_by_loop.values())
        for client in list(clients_for_loop.values())
    ]
    for client in clients:
        if not getattr(client, "is_closed", False):
            await client.aclose()


async def _read_http_request(reader: asyncio.StreamReader) -> tuple[str, dict[str, str], dict[str, Any]]:
    header_bytes = await reader.readuntil(b"\r\n\r\n")
    header_text = header_bytes.decode("iso-8859-1")
    lines = header_text.split("\r\n")
    method, path, _version = lines[0].split(" ", 2)
    assert method == "POST"
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        name, value = line.split(":", 1)
        headers[name.lower()] = value.strip()
    body_bytes = await reader.readexactly(int(headers.get("content-length", "0")))
    body = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
    return path, headers, body


def _response_for(path: str, body: dict[str, Any]) -> dict[str, Any]:
    if path == "/v1/chains":
        return {"id": f"chain-{body['external_chain_id']}"}
    if path.endswith("/events"):
        return {"policy_decision": "allow", "decision_source": "backend_evaluation"}
    if path.endswith("/complete"):
        return {"status": "completed"}
    return {"ok": True}


async def _write_json_response(
    writer: asyncio.StreamWriter,
    status: int,
    payload: dict[str, Any],
) -> None:
    body = json.dumps(payload).encode("utf-8")
    writer.write(
        b"\r\n".join(
            [
                f"HTTP/1.1 {status} OK".encode("ascii"),
                b"Content-Type: application/json",
                f"Content-Length: {len(body)}".encode("ascii"),
                b"Connection: close",
                b"",
                body,
            ]
        )
    )
    await writer.drain()


@pytest_asyncio.fixture
async def local_backend():
    backend = LocalBackend(server=None, base_url="")  # type: ignore[arg-type]

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            path, headers, body = await _read_http_request(reader)
            auth = headers.get("authorization", "")
            backend.records.append(RecordedRequest(path=path, authorization=auth, body=body))

            label = str(body.get("external_chain_id") or body.get("label") or "")
            if label.startswith("A") and auth != "Bearer prail_A":
                backend.wrong_credentials += 1
            if label.startswith("B") and auth != "Bearer prail_B":
                backend.wrong_credentials += 1

            if path in backend.block_paths:
                backend.blocked.set()
                await backend.release.wait()

            await _write_json_response(writer, 200, _response_for(path, body))
        except Exception:
            backend.lifecycle_errors += 1
            with contextlib.suppress(Exception):
                await _write_json_response(writer, 500, {"detail": "test server error"})
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    backend.server = server
    backend.base_url = f"http://127.0.0.1:{port}"
    _client._reset_for_tests()
    try:
        yield backend
    finally:
        await _close_cached_clients()
        _client._reset_for_tests()
        await backend.close()


@pytest.mark.asyncio
async def test_in_flight_child_request_survives_parent_context_reinit(local_backend):
    arclasp.init(api_key="prail_A", backend_url=local_backend.base_url)
    chain_a = Chain("A-inflight")
    config_a = _client._snapshot_config()
    client_a = _client._get_client(config_a)
    local_backend.block_paths.add("/v1/chains")

    child = asyncio.create_task(chain_a._start())
    await asyncio.wait_for(local_backend.blocked.wait(), timeout=5)

    arclasp.init(api_key="prail_B", backend_url=local_backend.base_url)
    assert client_a.is_closed is False
    local_backend.release.set()
    await child

    assert chain_a.chain_id == "chain-A-inflight"
    assert local_backend.auths_for("A-inflight") == ["Bearer prail_A"]
    assert _client.get_config().api_key.get_secret_value() == "prail_B"
    assert local_backend.wrong_credentials == 0
    assert local_backend.lifecycle_errors == 0


@pytest.mark.asyncio
async def test_child_inherited_context_keeps_a_after_parent_reinit(local_backend):
    arclasp.init(api_key="prail_A", backend_url=local_backend.base_url)
    ready = asyncio.Event()
    go = asyncio.Event()

    async def child() -> None:
        ready.set()
        await go.wait()
        async with Chain("A-child"):
            pass

    task = asyncio.create_task(child())
    await ready.wait()
    arclasp.init(api_key="prail_B", backend_url=local_backend.base_url)
    go.set()
    async with Chain("B-parent"):
        pass
    await task

    assert set(local_backend.auths_for("A-child")) == {"Bearer prail_A"}
    assert set(local_backend.auths_for("B-parent")) == {"Bearer prail_B"}
    assert local_backend.wrong_credentials == 0
    assert local_backend.lifecycle_errors == 0


@pytest.mark.asyncio
async def test_bound_chains_remain_usable_across_same_task_reinit(local_backend):
    arclasp.init(api_key="prail_A", backend_url=local_backend.base_url)
    chain_a = Chain("A-bound")
    arclasp.init(api_key="prail_B", backend_url=local_backend.base_url)
    chain_b = Chain("B-bound")

    await chain_a._start()
    await chain_b._start()
    await chain_a.record_agent_action("agent-a", "tool_call", "A-event")

    assert set(local_backend.auths_for("A-bound")) == {"Bearer prail_A"}
    assert set(local_backend.auths_for("B-bound")) == {"Bearer prail_B"}
    assert local_backend.wrong_credentials == 0
    assert local_backend.lifecycle_errors == 0


@pytest.mark.asyncio
async def test_shared_config_child_tasks_survive_parent_reinit(local_backend):
    arclasp.init(api_key="prail_A", backend_url=local_backend.base_url)
    local_backend.block_paths.add("/v1/chains")

    first = asyncio.create_task(Chain("A-first")._start())
    await asyncio.wait_for(local_backend.blocked.wait(), timeout=5)

    second_ready = asyncio.Event()

    async def second_child() -> None:
        second_ready.set()
        async with Chain("A-second"):
            pass

    second = asyncio.create_task(second_child())
    await second_ready.wait()
    arclasp.init(api_key="prail_B", backend_url=local_backend.base_url)
    local_backend.release.set()
    await asyncio.gather(first, second)

    assert set(local_backend.auths_for("A-first")) == {"Bearer prail_A"}
    assert set(local_backend.auths_for("A-second")) == {"Bearer prail_A"}
    assert local_backend.wrong_credentials == 0
    assert local_backend.lifecycle_errors == 0


@pytest.mark.asyncio
async def test_reinit_churn_keeps_previously_bound_chains_usable(local_backend):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        arclasp.init(api_key="prail_A", backend_url=local_backend.base_url)
        chain_a1 = Chain("A-one")
        arclasp.init(api_key="prail_B", backend_url=local_backend.base_url)
        chain_b1 = Chain("B-one")
        arclasp.init(api_key="prail_A", backend_url=local_backend.base_url)
        chain_a2 = Chain("A-two")
        arclasp.init(api_key="prail_B", backend_url=local_backend.base_url)
        chain_b2 = Chain("B-two")

        await chain_a1._start()
        await chain_b1._start()
        await chain_a2._start()
        await chain_b2._start()
        await chain_a1._complete()
        await chain_b1._complete()
        await chain_a2._complete()
        await chain_b2._complete()

    assert set(local_backend.auths_for("A-one")) == {"Bearer prail_A"}
    assert set(local_backend.auths_for("A-two")) == {"Bearer prail_A"}
    assert set(local_backend.auths_for("B-one")) == {"Bearer prail_B"}
    assert set(local_backend.auths_for("B-two")) == {"Bearer prail_B"}
    assert local_backend.wrong_credentials == 0
    assert local_backend.lifecycle_errors == 0
    assert [w for w in caught if issubclass(w.category, ResourceWarning)] == []
