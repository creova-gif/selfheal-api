from __future__ import annotations

import asyncio
import concurrent.futures
import io
import json
import threading
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from selfheal_api.middleware import (
    RouteBinding,
    SelfHealASGIMiddleware,
    SelfHealWSGIMiddleware,
)
from sentinel import DeterministicMappingProvider, DriftDeduplicator, DriftReport


class WebhookOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    amount: int


async def _run_asgi_request(
    app: SelfHealASGIMiddleware,
    scope: dict[str, Any],
    chunks: list[bytes],
) -> tuple[dict[str, Any], bytes]:
    idx = 0

    async def receive() -> dict[str, Any]:
        nonlocal idx
        if idx < len(chunks):
            chunk = chunks[idx]
            idx += 1
            return {
                "type": "http.request",
                "body": chunk,
                "more_body": idx < len(chunks),
            }
        return {"type": "http.disconnect"}

    async def send(_message: dict[str, Any]) -> None:
        return None

    await app(scope, receive, send)
    return scope, b""


def test_asgi_passthrough_non_http_and_unmatched_routes() -> None:
    seen_scopes: list[dict[str, Any]] = []
    receive_calls = 0

    async def downstream(
        scope: dict[str, Any],
        receive: Callable[..., Awaitable[dict[str, Any]]],
        send: Callable[..., Awaitable[None]],
    ) -> None:
        seen_scopes.append(scope)
        if scope["type"] == "http":
            msg = await receive()
            assert msg["body"] == b'{"raw":true}'

    drift_events: list[str] = []
    middleware = SelfHealASGIMiddleware(
        downstream,
        {"/webhooks/order": WebhookOrder},
        on_drift=lambda _rep, fp: drift_events.append(fp),
    )

    async def fake_receive() -> dict[str, Any]:
        nonlocal receive_calls
        receive_calls += 1
        return {"type": "http.request", "body": b'{"raw":true}', "more_body": False}

    async def fake_send(_msg: dict[str, Any]) -> None:
        return None

    # Non-HTTP scope passes straight through without touching receive
    asyncio.run(
        middleware(
            {"type": "websocket", "path": "/webhooks/order"},
            fake_receive,
            fake_send,
        )
    )
    assert receive_calls == 0

    # GET on bound path passes straight through
    asyncio.run(
        middleware(
            {"type": "http", "method": "GET", "path": "/webhooks/order"},
            fake_receive,
            fake_send,
        )
    )
    assert receive_calls == 1

    # POST on unbound path passes straight through
    asyncio.run(
        middleware(
            {"type": "http", "method": "POST", "path": "/other"},
            fake_receive,
            fake_send,
        )
    )
    assert receive_calls == 2
    assert len(seen_scopes) == 3
    assert drift_events == []


def test_asgi_drift_detection_normalization_and_deduplication() -> None:
    received_bodies: list[bytes] = []
    received_lengths: list[bytes | None] = []
    llm_calls = 0
    drift_events: list[str] = []

    def resolver(
        _model_name: str,
        _missing_fields: dict[str, str],
        _extra_observed_kinds: dict[str, str],
    ) -> dict[str, tuple[str | int, ...]]:
        nonlocal llm_calls
        llm_calls += 1
        return {"id": ("x_ref_alpha",)}

    provider = DeterministicMappingProvider(resolver)
    dedup = DriftDeduplicator(max_fingerprints=16)

    async def downstream(
        scope: dict[str, Any],
        receive: Callable[..., Awaitable[dict[str, Any]]],
        send: Callable[..., Awaitable[None]],
    ) -> None:
        msg = await receive()
        body = msg["body"]
        received_bodies.append(body)
        headers = dict(scope.get("headers", []))
        received_lengths.append(headers.get(b"content-length"))
        validated = WebhookOrder.model_validate_json(body, strict=True)
        assert validated.amount > 0
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    middleware = SelfHealASGIMiddleware(
        downstream,
        {"/webhooks/order": WebhookOrder},
        normalize=True,
        on_drift=lambda _rep, fp: drift_events.append(fp),
        mapping_provider=provider,
        deduplicator=dedup,
    )

    for idx in range(5):
        raw = json.dumps({"x_ref_alpha": f"ord_{idx}", "amount": str(idx + 10)}).encode("utf-8")
        midpoint = len(raw) // 2
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/webhooks/order",
            "headers": [(b"content-length", str(len(raw)).encode("ascii"))],
        }
        asyncio.run(_run_asgi_request(middleware, scope, [raw[:midpoint], raw[midpoint:]]))

    assert llm_calls == 1
    assert len(drift_events) == 1
    assert len(received_bodies) == 5
    for idx, body in enumerate(received_bodies):
        parsed = json.loads(body.decode("utf-8"))
        assert parsed == {"id": f"ord_{idx}", "amount": idx + 10}
        assert received_lengths[idx] == str(len(body)).encode("ascii")


def test_asgi_normalize_false_preserves_original_body() -> None:
    observed_bodies: list[bytes] = []
    drift_reports: list[DriftReport] = []

    async def downstream(
        _scope: dict[str, Any],
        receive: Callable[..., Awaitable[dict[str, Any]]],
        _send: Callable[..., Awaitable[None]],
    ) -> None:
        msg = await receive()
        observed_bodies.append(msg["body"])

    middleware = SelfHealASGIMiddleware(
        downstream,
        [RouteBinding(path="/webhooks/order", model_cls=WebhookOrder, normalize=False)],
        on_drift=lambda rep, _fp: drift_reports.append(rep),
    )

    raw = b'{"id": "ord_1", "amount": "42"}'
    scope = {"type": "http", "method": "PUT", "path": "/webhooks/order"}
    asyncio.run(_run_asgi_request(middleware, scope, [raw]))

    assert len(drift_reports) == 1
    assert drift_reports[0].has_drift is True
    assert observed_bodies == [raw]


def test_wsgi_passthrough_and_drift_normalization_deduplication() -> None:
    received_payloads: list[dict[str, Any]] = []
    received_lengths: list[str] = []
    llm_calls = 0
    drift_events: list[str] = []

    def resolver(
        _model_name: str,
        _missing_fields: dict[str, str],
        _extra_observed_kinds: dict[str, str],
    ) -> dict[str, tuple[str | int, ...]]:
        nonlocal llm_calls
        llm_calls += 1
        return {"id": ("x_ref_alpha",)}

    provider = DeterministicMappingProvider(resolver)
    dedup = DriftDeduplicator(max_fingerprints=16)

    def wsgi_app(environ: dict[str, Any], start_response: Any) -> Iterable[bytes]:
        length = int(environ.get("CONTENT_LENGTH") or 0)
        raw = environ["wsgi.input"].read(length)
        if raw:
            received_payloads.append(json.loads(raw.decode("utf-8")))
            received_lengths.append(environ["CONTENT_LENGTH"])
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"ok"]

    middleware = SelfHealWSGIMiddleware(
        wsgi_app,
        {"/webhooks/order": WebhookOrder},
        normalize=True,
        on_drift=lambda _rep, fp: drift_events.append(fp),
        mapping_provider=provider,
        deduplicator=dedup,
    )

    statuses: list[str] = []

    def start_response(status: str, _headers: list[tuple[str, str]]) -> None:
        statuses.append(status)

    # GET passthrough
    get_res = list(
        middleware(
            {
                "REQUEST_METHOD": "GET",
                "PATH_INFO": "/webhooks/order",
                "wsgi.input": io.BytesIO(b""),
                "CONTENT_LENGTH": "0",
            },
            start_response,
        )
    )
    assert get_res == [b"ok"]
    assert drift_events == []

    # Unmatched path passthrough
    other_body = b'{"anything": 1}'
    list(
        middleware(
            {
                "REQUEST_METHOD": "POST",
                "PATH_INFO": "/unmatched",
                "wsgi.input": io.BytesIO(other_body),
                "CONTENT_LENGTH": str(len(other_body)),
            },
            start_response,
        )
    )
    assert received_payloads[-1] == {"anything": 1}
    assert drift_events == []

    # Repeated drifted POSTs: LLM called once, on_drift once, all normalized
    for idx in range(4):
        raw = json.dumps({"x_ref_alpha": f"w_{idx}", "amount": str(idx + 5)}).encode("utf-8")
        res = list(
            middleware(
                {
                    "REQUEST_METHOD": "POST",
                    "PATH_INFO": "/webhooks/order",
                    "wsgi.input": io.BytesIO(raw),
                    "CONTENT_LENGTH": str(len(raw)),
                },
                start_response,
            )
        )
        assert res == [b"ok"]
        validated = WebhookOrder.model_validate(received_payloads[-1], strict=True)
        assert validated.id == f"w_{idx}"
        assert validated.amount == idx + 5
        expected_len = str(len(json.dumps(received_payloads[-1]).encode("utf-8")))
        assert received_lengths[-1] == expected_len

    assert llm_calls == 1
    assert len(drift_events) == 1
    assert len(statuses) == 6


def test_asgi_async_burst_small_threadpool_no_deadlock() -> None:
    llm_calls = 0
    received_bodies: list[bytes] = []

    def resolver(
        _model_name: str,
        _missing_fields: dict[str, str],
        _extra_observed_kinds: dict[str, str],
    ) -> dict[str, tuple[str | int, ...]]:
        nonlocal llm_calls
        llm_calls += 1
        time.sleep(0.03)
        return {"id": ("x_ref_alpha",)}

    provider = DeterministicMappingProvider(resolver)
    dedup = DriftDeduplicator(max_fingerprints=16)

    async def downstream(
        _scope: dict[str, Any],
        receive: Callable[..., Awaitable[dict[str, Any]]],
        send: Callable[..., Awaitable[None]],
    ) -> None:
        msg = await receive()
        body = msg["body"]
        WebhookOrder.model_validate_json(body, strict=True)
        received_bodies.append(body)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    middleware = SelfHealASGIMiddleware(
        downstream,
        {"/webhooks/order": WebhookOrder},
        normalize=True,
        mapping_provider=provider,
        deduplicator=dedup,
    )

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            loop.set_default_executor(pool)
            tasks = []
            for idx in range(10):
                raw = json.dumps(
                    {"x_ref_alpha": f"ord_{idx}", "amount": str(idx + 10)}
                ).encode("utf-8")
                scope = {
                    "type": "http",
                    "method": "POST",
                    "path": "/webhooks/order",
                    "headers": [(b"content-length", str(len(raw)).encode("ascii"))],
                }
                tasks.append(
                    asyncio.create_task(_run_asgi_request(middleware, scope, [raw]))
                )
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=5.0)

    asyncio.run(_run())
    assert llm_calls == 1
    assert len(received_bodies) == 10


def test_asgi_cancel_owner_releases_reservation() -> None:
    provider_entered = threading.Event()
    release_provider = threading.Event()
    llm_calls = 0
    received_bodies: list[bytes] = []

    def resolver(
        _model_name: str,
        _missing_fields: dict[str, str],
        _extra_observed_kinds: dict[str, str],
    ) -> dict[str, tuple[str | int, ...]]:
        nonlocal llm_calls
        llm_calls += 1
        if llm_calls == 1:
            provider_entered.set()
            release_provider.wait(timeout=2.0)
        return {"id": ("x_ref_alpha",)}

    provider = DeterministicMappingProvider(resolver)
    dedup = DriftDeduplicator(max_fingerprints=16)

    async def downstream(
        _scope: dict[str, Any],
        receive: Callable[..., Awaitable[dict[str, Any]]],
        _send: Callable[..., Awaitable[None]],
    ) -> None:
        msg = await receive()
        received_bodies.append(msg["body"])

    middleware = SelfHealASGIMiddleware(
        downstream,
        {"/webhooks/order": WebhookOrder},
        normalize=True,
        mapping_provider=provider,
        deduplicator=dedup,
    )

    async def _run() -> None:
        raw1 = b'{"x_ref_alpha": "ord_1", "amount": "10"}'
        scope1 = {"type": "http", "method": "POST", "path": "/webhooks/order"}
        owner_task = asyncio.create_task(_run_asgi_request(middleware, scope1, [raw1]))
        while not provider_entered.is_set():
            await asyncio.sleep(0.005)

        owner_task.cancel()
        release_provider.set()
        with pytest.raises(asyncio.CancelledError):
            await owner_task

        raw2 = b'{"x_ref_alpha": "ord_2", "amount": "20"}'
        scope2 = {"type": "http", "method": "POST", "path": "/webhooks/order"}
        await asyncio.wait_for(
            _run_asgi_request(middleware, scope2, [raw2]),
            timeout=2.0,
        )

    asyncio.run(_run())
    assert llm_calls == 2
    assert len(received_bodies) == 1
    assert json.loads(received_bodies[0].decode("utf-8")) == {
        "id": "ord_2",
        "amount": 20,
    }


def test_asgi_cancel_duplicate_waiter_does_not_abort_owner() -> None:
    provider_entered = threading.Event()
    release_provider = threading.Event()
    llm_calls = 0
    received_bodies: list[bytes] = []

    def resolver(
        _model_name: str,
        _missing_fields: dict[str, str],
        _extra_observed_kinds: dict[str, str],
    ) -> dict[str, tuple[str | int, ...]]:
        nonlocal llm_calls
        llm_calls += 1
        provider_entered.set()
        release_provider.wait(timeout=2.0)
        return {"id": ("x_ref_alpha",)}

    provider = DeterministicMappingProvider(resolver)
    dedup = DriftDeduplicator(max_fingerprints=16)

    async def downstream(
        _scope: dict[str, Any],
        receive: Callable[..., Awaitable[dict[str, Any]]],
        _send: Callable[..., Awaitable[None]],
    ) -> None:
        msg = await receive()
        received_bodies.append(msg["body"])

    middleware = SelfHealASGIMiddleware(
        downstream,
        {"/webhooks/order": WebhookOrder},
        normalize=True,
        mapping_provider=provider,
        deduplicator=dedup,
    )

    async def _run() -> None:
        raw_owner = b'{"x_ref_alpha": "ord_owner", "amount": "11"}'
        scope_owner = {"type": "http", "method": "POST", "path": "/webhooks/order"}
        owner_task = asyncio.create_task(
            _run_asgi_request(middleware, scope_owner, [raw_owner])
        )
        while not provider_entered.is_set():
            await asyncio.sleep(0.005)

        raw_waiter = b'{"x_ref_alpha": "ord_waiter", "amount": "12"}'
        scope_waiter = {"type": "http", "method": "POST", "path": "/webhooks/order"}
        waiter_task = asyncio.create_task(
            _run_asgi_request(middleware, scope_waiter, [raw_waiter])
        )
        await asyncio.sleep(0.01)
        assert not waiter_task.done()

        waiter_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter_task

        release_provider.set()
        await asyncio.wait_for(owner_task, timeout=2.0)

        raw_cached = b'{"x_ref_alpha": "ord_cached", "amount": "13"}'
        scope_cached = {"type": "http", "method": "POST", "path": "/webhooks/order"}
        await asyncio.wait_for(
            _run_asgi_request(middleware, scope_cached, [raw_cached]),
            timeout=2.0,
        )

    asyncio.run(_run())
    assert llm_calls == 1
    assert len(received_bodies) == 2
    assert json.loads(received_bodies[0].decode("utf-8")) == {
        "id": "ord_owner",
        "amount": 11,
    }
    assert json.loads(received_bodies[1].decode("utf-8")) == {
        "id": "ord_cached",
        "amount": 13,
    }
