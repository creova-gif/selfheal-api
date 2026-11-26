from __future__ import annotations

import asyncio
import io
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from pydantic import BaseModel

from sentinel.detector import DriftReport, detect_drift
from sentinel.guard import (
    DriftDeduplicator,
    _normalize_raw_payload,
    _safe_invoke_callback,
    compute_drift_fingerprint,
)
from sentinel.llm_fallback import SchemaMappingProvider
from synthesizer.patcher import build_patch_plan

ASGIApp = Callable[
    [
        dict[str, Any],
        Callable[..., Awaitable[dict[str, Any]]],
        Callable[..., Awaitable[None]],
    ],
    Awaitable[None],
]
WSGIStartResponse = Callable[..., Any]
WSGIApp = Callable[[dict[str, Any], WSGIStartResponse], Iterable[bytes]]


class RouteBinding(BaseModel, frozen=True):
    """Binding between an HTTP path/method set and a target Pydantic model."""

    path: str
    model_cls: type[BaseModel]
    methods: frozenset[str] = frozenset({"POST", "PUT", "PATCH"})
    normalize: bool = True


def _build_route_index(
    routes: dict[str, type[BaseModel]] | list[RouteBinding],
    *,
    default_normalize: bool,
) -> dict[tuple[str, str], RouteBinding]:
    bindings = (
        [
            RouteBinding(path=path, model_cls=model_cls, normalize=default_normalize)
            for path, model_cls in routes.items()
        ]
        if isinstance(routes, dict)
        else routes
    )
    index: dict[tuple[str, str], RouteBinding] = {}
    for binding in bindings:
        for method in binding.methods:
            index[(binding.path, method.upper())] = binding
    return index


def _update_asgi_content_length(scope: dict[str, Any], new_length: int) -> dict[str, Any]:
    raw_headers = scope.get("headers")
    if not isinstance(raw_headers, (list, tuple)):
        return scope
    updated_headers = [
        (b"content-length", str(new_length).encode("ascii"))
        if isinstance(name, bytes) and name.lower() == b"content-length"
        else (name, value)
        for name, value in raw_headers
    ]
    updated_scope = dict(scope)
    updated_scope["headers"] = updated_headers
    return updated_scope


def _maybe_normalize_bytes(
    body: bytes,
    binding: RouteBinding,
    plan: Any,
    *,
    normalize: bool,
) -> bytes:
    if not (normalize and binding.normalize and plan is not None):
        return body
    normalized = _normalize_raw_payload(body, binding.model_cls, plan)
    return normalized if isinstance(normalized, bytes) else body


class SelfHealASGIMiddleware:
    """Framework-agnostic ASGI middleware for schema-drift detection and normalization."""

    def __init__(
        self,
        app: ASGIApp,
        routes: dict[str, type[BaseModel]] | list[RouteBinding],
        *,
        normalize: bool = True,
        on_drift: Callable[[DriftReport, str], None] | None = None,
        mapping_provider: SchemaMappingProvider | None = None,
        deduplicator: DriftDeduplicator | None = None,
    ) -> None:
        self.app = app
        self._normalize = normalize
        self._on_drift = on_drift
        self._mapping_provider = mapping_provider
        self._deduplicator = deduplicator or DriftDeduplicator()
        self._route_index = _build_route_index(routes, default_normalize=normalize)

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[..., Awaitable[dict[str, Any]]],
        send: Callable[..., Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path", ""))
        method = str(scope.get("method", "GET")).upper()
        binding = self._route_index.get((path, method))
        if binding is None:
            await self.app(scope, receive, send)
            return

        chunks: list[bytes] = []
        more_body = True
        disconnect_msg: dict[str, Any] | None = None
        while more_body:
            message = await receive()
            msg_type = message.get("type")
            if msg_type == "http.disconnect":
                disconnect_msg = message
                break
            if msg_type == "http.request":
                chunk = message.get("body", b"")
                if isinstance(chunk, (bytes, bytearray)):
                    chunks.append(bytes(chunk))
                more_body = bool(message.get("more_body", False))

        body = b"".join(chunks)
        if body:
            report = detect_drift(binding.model_cls, body, mapping_provider=None)
            if report.has_drift:
                pre_fp = compute_drift_fingerprint(report)
                is_new, plan = await asyncio.to_thread(self._deduplicator.begin, pre_fp)
                if is_new:
                    try:
                        enriched = (
                            await asyncio.to_thread(
                                detect_drift,
                                binding.model_cls,
                                body,
                                self._mapping_provider,
                            )
                            if self._mapping_provider is not None
                            else report
                        )
                        plan = build_patch_plan(enriched, root_model=binding.model_cls)
                        self._deduplicator.complete(pre_fp, plan)
                    except BaseException:
                        self._deduplicator.abort(pre_fp)
                        raise
                    _safe_invoke_callback(
                        self._on_drift, enriched, compute_drift_fingerprint(enriched)
                    )
                body = _maybe_normalize_bytes(
                    body, binding, plan, normalize=self._normalize
                )

        forwarded_scope = _update_asgi_content_length(scope, len(body))
        body_sent = False

        async def replay_receive() -> dict[str, Any]:
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            if disconnect_msg is not None:
                return disconnect_msg
            return await receive()

        await self.app(forwarded_scope, replay_receive, send)


class SelfHealWSGIMiddleware:
    """Framework-agnostic WSGI middleware for schema-drift detection and normalization."""

    def __init__(
        self,
        app: WSGIApp,
        routes: dict[str, type[BaseModel]] | list[RouteBinding],
        *,
        normalize: bool = True,
        on_drift: Callable[[DriftReport, str], None] | None = None,
        mapping_provider: SchemaMappingProvider | None = None,
        deduplicator: DriftDeduplicator | None = None,
    ) -> None:
        self.app = app
        self._normalize = normalize
        self._on_drift = on_drift
        self._mapping_provider = mapping_provider
        self._deduplicator = deduplicator or DriftDeduplicator()
        self._route_index = _build_route_index(routes, default_normalize=normalize)

    def __call__(
        self,
        environ: dict[str, Any],
        start_response: WSGIStartResponse,
    ) -> Iterable[bytes]:
        path = str(environ.get("PATH_INFO", ""))
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        binding = self._route_index.get((path, method))
        if binding is None:
            return self.app(environ, start_response)

        wsgi_input = environ.get("wsgi.input")
        if wsgi_input is None:
            return self.app(environ, start_response)

        raw_length = environ.get("CONTENT_LENGTH")
        if raw_length not in (None, ""):
            try:
                content_length = int(raw_length)
            except (TypeError, ValueError):
                content_length = 0
            raw_body = wsgi_input.read(max(0, content_length))
        else:
            raw_body = wsgi_input.read()

        body = raw_body.encode("utf-8") if isinstance(raw_body, str) else bytes(raw_body)
        if body:
            report = detect_drift(binding.model_cls, body, mapping_provider=None)
            if report.has_drift:
                pre_fp = compute_drift_fingerprint(report)
                is_new, plan = self._deduplicator.begin(pre_fp)
                if is_new:
                    try:
                        enriched = (
                            detect_drift(
                                binding.model_cls,
                                body,
                                mapping_provider=self._mapping_provider,
                            )
                            if self._mapping_provider is not None
                            else report
                        )
                        plan = build_patch_plan(enriched, root_model=binding.model_cls)
                        self._deduplicator.complete(pre_fp, plan)
                    except BaseException:
                        self._deduplicator.abort(pre_fp)
                        raise
                    _safe_invoke_callback(
                        self._on_drift, enriched, compute_drift_fingerprint(enriched)
                    )
                body = _maybe_normalize_bytes(
                    body, binding, plan, normalize=self._normalize
                )

        updated_environ = dict(environ)
        updated_environ["wsgi.input"] = io.BytesIO(body)
        updated_environ["CONTENT_LENGTH"] = str(len(body))
        return self.app(updated_environ, start_response)
