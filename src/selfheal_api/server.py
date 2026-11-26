from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sentinel.detector import detect_drift
from sentinel.guard import DriftDeduplicator, compute_drift_fingerprint
from sentinel.llm_fallback import AnthropicSchemaMappingProvider
from synthesizer.patcher import PatchPlan, build_patch_plan
from synthesizer.zod_emitter import synthesize_zod_preprocessor

_logger = logging.getLogger("selfheal_api.server")

SERVER_VERSION = "0.2.0"
_ORG_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class TenantConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    webhook_secret: str = ""
    max_requests_per_minute: int = Field(default=60, ge=1)
    max_llm_calls_per_tenant: int = Field(default=20, ge=0)
    max_fingerprints: int = Field(default=1024, ge=1)
    enable_llm_fallback: bool = False


class ServerConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=0, le=65535)
    webhook_secret: str = ""
    max_requests_per_minute: int = Field(default=60, ge=1)
    max_llm_calls_per_tenant: int = Field(default=20, ge=0)
    tenants: dict[str, TenantConfig] = Field(default_factory=dict)
    allow_unregistered_tenants: bool = True
    include_zod_adapter: bool = True
    max_body_bytes: int = Field(default=1_048_576, ge=1024)

    def default_tenant_config(self) -> TenantConfig:
        return TenantConfig(
            webhook_secret=self.webhook_secret,
            max_requests_per_minute=self.max_requests_per_minute,
            max_llm_calls_per_tenant=self.max_llm_calls_per_tenant,
        )


class DriftWebhookRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1, max_length=128)
    payload: dict[str, Any] | str
    include_zod_adapter: bool | None = None


class TokenBucketRateLimiter:
    """Thread-safe per-tenant token-bucket rate limiter."""

    def __init__(
        self,
        max_requests_per_minute: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_requests_per_minute < 1:
            raise ValueError("max_requests_per_minute must be >= 1")
        self._capacity = float(max_requests_per_minute)
        self._tokens = float(max_requests_per_minute)
        self._refill_rate = max_requests_per_minute / 60.0
        self._clock = clock
        self._last_refill = self._clock()
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            now = self._clock()
            elapsed = max(0.0, now - self._last_refill)
            self._tokens = min(
                self._capacity, self._tokens + elapsed * self._refill_rate
            )
            self._last_refill = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False


class TenantState:
    """Isolated per-tenant runtime state (deduplicator, rate limiter, LLM cap)."""

    def __init__(self, org_id: str, config: TenantConfig) -> None:
        self.org_id = org_id
        self.config = config
        self.deduplicator = DriftDeduplicator(max_fingerprints=config.max_fingerprints)
        self.rate_limiter = TokenBucketRateLimiter(
            max_requests_per_minute=config.max_requests_per_minute
        )
        self.mapping_provider: AnthropicSchemaMappingProvider | None = (
            AnthropicSchemaMappingProvider(max_calls=config.max_llm_calls_per_tenant)
            if config.enable_llm_fallback
            else None
        )


class TenantRegistry:
    """Thread-safe registry maintaining strict per-org_id isolation."""

    def __init__(self, config: ServerConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._tenants: dict[str, TenantState] = {
            org_id: TenantState(org_id=org_id, config=tenant_cfg)
            for org_id, tenant_cfg in config.tenants.items()
        }

    def get_or_create(self, org_id: str) -> TenantState | None:
        with self._lock:
            existing = self._tenants.get(org_id)
            if existing is not None:
                return existing
            if not self._config.allow_unregistered_tenants:
                return None
            state = TenantState(
                org_id=org_id,
                config=self._config.default_tenant_config(),
            )
            self._tenants[org_id] = state
            return state

    def get_tenant(self, org_id: str) -> TenantState | None:
        with self._lock:
            return self._tenants.get(org_id)


def verify_webhook_signature(
    secret: str, body: bytes, signature_header: str
) -> bool:
    """Verify HMAC-SHA256 webhook signature in `sha256=<hex>` format."""
    if not secret or not signature_header:
        return False
    header = signature_header.strip()
    if not header.lower().startswith("sha256="):
        return False
    provided_sig = f"sha256={header[7:].lower()}"
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    expected_sig = f"sha256={digest}"
    return hmac.compare_digest(expected_sig, provided_sig)


class SelfHealHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        RequestHandlerClass: type[BaseHTTPRequestHandler],
        config: ServerConfig,
        registry: TenantRegistry,
        model_registry: Mapping[str, type[BaseModel]],
    ) -> None:
        self.config = config
        self.registry = registry
        self.model_registry: dict[str, type[BaseModel]] = dict(model_registry)
        super().__init__(server_address, RequestHandlerClass)


class SelfHealRequestHandler(BaseHTTPRequestHandler):
    server: SelfHealHTTPServer

    def log_message(self, format: str, *args: Any) -> None:
        _logger.info("%s - %s", self.address_string(), format % args)

    def _send_json(
        self,
        status: HTTPStatus,
        payload: dict[str, Any],
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self._send_json(
                HTTPStatus.OK,
                {"status": "ok", "version": SERVER_VERSION},
            )
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path != "/webhooks/drift":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        self._handle_drift_webhook()

    def _read_body(self) -> bytes | None:
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "missing Content-Length header"}
            )
            return None
        try:
            content_length = int(raw_len)
        except ValueError:
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "invalid Content-Length header"}
            )
            return None
        if content_length < 0:
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "invalid Content-Length header"}
            )
            return None
        if content_length > self.server.config.max_body_bytes:
            self._send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": "request body exceeds maximum allowed size"},
            )
            return None
        return self.rfile.read(content_length)

    def _handle_drift_webhook(self) -> None:
        org_id_raw = self.headers.get("X-SelfHeal-Org", "").strip()
        if not org_id_raw or not _ORG_ID_RE.match(org_id_raw):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "missing or invalid X-SelfHeal-Org header"},
            )
            return

        tenant = self.server.registry.get_or_create(org_id_raw)
        if tenant is None:
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"error": f"unknown organization: {org_id_raw}"},
            )
            return

        raw_body = self._read_body()
        if raw_body is None:
            return

        if tenant.config.webhook_secret:
            sig_header = (
                self.headers.get("X-Hub-Signature-256")
                or self.headers.get("X-SelfHeal-Signature")
                or ""
            )
            if not verify_webhook_signature(
                tenant.config.webhook_secret, raw_body, sig_header
            ):
                self._send_json(
                    HTTPStatus.UNAUTHORIZED,
                    {"error": "invalid or missing webhook signature"},
                )
                return

        if not tenant.rate_limiter.allow():
            self._send_json(
                HTTPStatus.TOO_MANY_REQUESTS,
                {"error": "rate limit exceeded"},
                extra_headers={"Retry-After": "60"},
            )
            return

        try:
            decoded = raw_body.decode("utf-8")
            parsed_json = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "invalid JSON request body"}
            )
            return

        if not isinstance(parsed_json, dict):
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "request body must be a JSON object"}
            )
            return

        try:
            req = DriftWebhookRequest.model_validate(parsed_json)
        except ValidationError as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid webhook request schema", "details": exc.errors()},
            )
            return

        expected_model = self.server.model_registry.get(req.model)
        if expected_model is None:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": f"unknown model: {req.model}"},
            )
            return

        report = detect_drift(
            expected_model,
            req.payload,
            mapping_provider=tenant.mapping_provider,
        )
        fingerprint = compute_drift_fingerprint(report)

        is_new, cached_plan = tenant.deduplicator.begin(fingerprint)
        plan: PatchPlan | None = cached_plan
        if is_new:
            try:
                plan = build_patch_plan(report, root_model=expected_model)
                tenant.deduplicator.complete(fingerprint, plan)
            except Exception:
                tenant.deduplicator.abort(fingerprint)
                raise
        elif plan is None:
            plan = build_patch_plan(report, root_model=expected_model)

        include_zod = (
            req.include_zod_adapter
            if req.include_zod_adapter is not None
            else self.server.config.include_zod_adapter
        )
        zod_adapter: str | None = None
        if include_zod and plan.actions:
            zod_adapter = synthesize_zod_preprocessor(plan)

        _logger.info(
            "drift_webhook org_id=%s model=%s has_drift=%s fingerprint=%s deduplicated=%s",
            org_id_raw,
            req.model,
            report.has_drift,
            fingerprint,
            not is_new,
        )

        self._send_json(
            HTTPStatus.OK,
            {
                "org_id": org_id_raw,
                "model": req.model,
                "has_drift": report.has_drift,
                "fingerprint": fingerprint,
                "deduplicated": not is_new,
                "issues": [issue.model_dump(mode="json") for issue in report.issues],
                "zod_adapter": zod_adapter,
            },
        )


def create_server(
    host: str = "0.0.0.0",
    port: int = 8080,
    config: ServerConfig | None = None,
    model_registry: Mapping[str, type[BaseModel]] | None = None,
) -> SelfHealHTTPServer:
    resolved_config = config or ServerConfig(host=host, port=port)
    registry = TenantRegistry(resolved_config)
    return SelfHealHTTPServer(
        (host, port),
        SelfHealRequestHandler,
        config=resolved_config,
        registry=registry,
        model_registry=model_registry or {},
    )


def _load_config_from_env_and_file(config_path: str | None) -> ServerConfig:
    if config_path:
        raw = json.loads(Path(config_path).read_text(encoding="utf-8"))
        return ServerConfig.model_validate(raw)
    port_env = int(os.environ.get("PORT", "8080"))
    host_env = os.environ.get("SELFHEAL_HOST", "0.0.0.0")
    secret_env = os.environ.get("SELFHEAL_WEBHOOK_SECRET", "")
    rpm_env = int(os.environ.get("SELFHEAL_MAX_RPM", "60"))
    llm_cap_env = int(os.environ.get("SELFHEAL_MAX_LLM_CALLS", "20"))
    return ServerConfig(
        host=host_env,
        port=port_env,
        webhook_secret=secret_env,
        max_requests_per_minute=rpm_env,
        max_llm_calls_per_tenant=llm_cap_env,
    )


def main_server(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="selfheal-server",
        description="Hosted multi-tenant selfheal-api drift webhook server",
    )
    parser.add_argument("--host", default=None, help="Bind host (default: 0.0.0.0)")
    parser.add_argument(
        "--port", type=int, default=None, help="Bind port (default: $PORT or 8080)"
    )
    parser.add_argument(
        "--config", default=None, help="Optional path to JSON ServerConfig file"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = _load_config_from_env_and_file(args.config)
    host = args.host if args.host is not None else config.host
    port = args.port if args.port is not None else config.port

    server = create_server(host=host, port=port, config=config)
    _logger.info("Starting selfheal-server on %s:%d", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _logger.info("Shutting down selfheal-server")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main_server())
