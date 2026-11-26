from __future__ import annotations

import argparse
import hmac
import importlib
import json
import logging
import os
import re
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from selfheal_api.billing import BillingStore, handle_stripe_webhook
from selfheal_api.dashboard import DashboardSnapshot, render_dashboard_html
from selfheal_api.github_app import (
    InstallationRegistry,
    handle_github_webhook,
)
from selfheal_api.github_app import (
    verify_webhook_signature as verify_webhook_signature,
)
from selfheal_api.ledger import DriftLedger
from selfheal_api.notifier import AlertNotifier, DriftAlert
from sentinel.detector import detect_drift
from sentinel.guard import DriftDeduplicator, compute_drift_fingerprint
from sentinel.llm_fallback import AnthropicSchemaMappingProvider
from synthesizer.patcher import PatchPlan, build_patch_plan
from synthesizer.zod_emitter import synthesize_zod_preprocessor

_logger = logging.getLogger("selfheal_api.server")

SERVER_VERSION = "0.2.0"
_ORG_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")


class TenantConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    webhook_secret: str = ""
    stripe_webhook_secret: str = ""
    github_webhook_secret: str = ""
    max_requests_per_minute: int = Field(default=60, ge=1)
    max_llm_calls_per_tenant: int = Field(default=20, ge=0)
    max_fingerprints: int = Field(default=1024, ge=1)
    enable_llm_fallback: bool = False


class ServerConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=0, le=65535)
    webhook_secret: str = ""
    stripe_webhook_secret: str = ""
    github_webhook_secret: str = ""
    max_requests_per_minute: int = Field(default=60, ge=1)
    max_llm_calls_per_tenant: int = Field(default=20, ge=0)
    tenants: dict[str, TenantConfig] = Field(default_factory=dict)
    allow_unregistered_tenants: bool = True
    include_zod_adapter: bool = True
    max_body_bytes: int = Field(default=1_048_576, ge=1024)
    db_path: str = ":memory:"

    def default_tenant_config(self) -> TenantConfig:
        opts: dict[str, Any] = {
            "webhook_secret": self.webhook_secret,
            "max_requests_per_minute": self.max_requests_per_minute,
            "max_llm_calls_per_tenant": self.max_llm_calls_per_tenant,
        }
        return TenantConfig(**opts)


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

    def update_limit(self, max_requests_per_minute: int) -> None:
        if max_requests_per_minute < 1:
            raise ValueError("max_requests_per_minute must be >= 1")
        with self._lock:
            new_capacity = float(max_requests_per_minute)
            if new_capacity != self._capacity:
                self._capacity = new_capacity
                self._refill_rate = max_requests_per_minute / 60.0
                self._tokens = min(self._tokens, self._capacity)


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

    def __init__(self, config: ServerConfig, max_tenants: int = 4096) -> None:
        self._config = config
        self._max_tenants = max_tenants
        self._lock = threading.Lock()
        self._tenants: OrderedDict[str, TenantState] = OrderedDict(
            (org_id, TenantState(org_id=org_id, config=tenant_cfg))
            for org_id, tenant_cfg in config.tenants.items()
        )

    def get_or_create(self, org_id: str) -> TenantState | None:
        with self._lock:
            existing = self._tenants.get(org_id)
            if existing is not None:
                self._tenants.move_to_end(org_id)
                return existing
            if not self._config.allow_unregistered_tenants:
                return None
            state = TenantState(
                org_id=org_id,
                config=self._config.default_tenant_config(),
            )
            self._tenants[org_id] = state
            while len(self._tenants) > self._max_tenants:
                self._tenants.popitem(last=False)
            return state

    def get_tenant(self, org_id: str) -> TenantState | None:
        with self._lock:
            return self._tenants.get(org_id)

    def list_org_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._tenants.keys())


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
        billing_store: BillingStore | None = None,
        installation_registry: InstallationRegistry | None = None,
        drift_ledger: DriftLedger | None = None,
        notifier: AlertNotifier | None = None,
    ) -> None:
        self.config = config
        self.registry = registry
        self.model_registry: dict[str, type[BaseModel]] = dict(model_registry)
        self.billing_store = billing_store or BillingStore()
        self.installation_registry = installation_registry or InstallationRegistry()
        self.drift_ledger = drift_ledger or DriftLedger()
        self.notifier = notifier
        super().__init__(server_address, RequestHandlerClass)


class SelfHealRequestHandler(BaseHTTPRequestHandler):
    server: SelfHealHTTPServer
    timeout = 10.0

    def _authorize_tenant_read(self, org_id: str) -> bool:
        tenant = self.server.registry.get_tenant(org_id)
        secret = (
            tenant.config.webhook_secret
            if tenant is not None
            else self.server.config.webhook_secret
        )
        if not secret:
            return True
        auth_hdr = self.headers.get("Authorization", "").strip()
        if auth_hdr.startswith("Bearer ") and hmac.compare_digest(
            auth_hdr[7:].strip(), secret
        ):
            return True
        sig_hdr = (
            self.headers.get("X-Hub-Signature-256")
            or self.headers.get("X-SelfHeal-Signature")
            or ""
        )
        return verify_webhook_signature(secret, org_id.encode("utf-8"), sig_hdr)

    def log_message(self, format: str, *args: Any) -> None:
        _logger.info("%s - %s", self.address_string(), format % args)

    def _get_request_id(self) -> str:
        cached: str | None = getattr(self, "_request_id", None)
        if cached is not None:
            return cached
        raw_hdr = self.headers.get("X-Request-Id", "").strip()
        if raw_hdr and _REQUEST_ID_RE.match(raw_hdr):
            request_id = raw_hdr
        else:
            request_id = uuid.uuid4().hex[:16]
        self._request_id = request_id
        return request_id

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
        self.send_header("X-Request-Id", self._get_request_id())
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: HTTPStatus, html_body: str) -> None:
        body = html_body.encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-Id", self._get_request_id())
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'",
        )
        self.end_headers()
        self.wfile.write(body)

    def _send_text(
        self,
        status: HTTPStatus,
        text_body: str,
        content_type: str = "text/plain; charset=utf-8",
    ) -> None:
        body = text_body.encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-Id", self._get_request_id())
        self.end_headers()
        self.wfile.write(body)

    def _format_prometheus_metrics(self) -> str:
        tenants_active = len(self.server.registry.list_org_ids())
        installations_active = len(self.server.installation_registry.list_org_ids())
        subscriptions_active = len(self.server.billing_store.list_org_ids())
        totals = self.server.drift_ledger.global_totals()
        lines = [
            "# HELP selfheal_tenants_active Number of active tenant states",
            "# TYPE selfheal_tenants_active gauge",
            f"selfheal_tenants_active {tenants_active}",
            "# HELP selfheal_installations_active Number of active GitHub installations",
            "# TYPE selfheal_installations_active gauge",
            f"selfheal_installations_active {installations_active}",
            "# HELP selfheal_subscriptions_active Number of active billing subscriptions",
            "# TYPE selfheal_subscriptions_active gauge",
            f"selfheal_subscriptions_active {subscriptions_active}",
            "# HELP selfheal_drift_events_total Total drift webhook events recorded",
            "# TYPE selfheal_drift_events_total counter",
            f"selfheal_drift_events_total {totals['total_events']}",
            "# HELP selfheal_drifted_events_total Total drift events with detected drift",
            "# TYPE selfheal_drifted_events_total counter",
            f"selfheal_drifted_events_total {totals['drifted_events']}",
            "# HELP selfheal_prs_opened_total Total remediation PRs opened",
            "# TYPE selfheal_prs_opened_total counter",
            f"selfheal_prs_opened_total {totals['prs_opened']}",
            "",
        ]
        return "\n".join(lines)

    def _query_org_id(self, query: dict[str, list[str]]) -> str:
        org_vals = query.get("org_id")
        if org_vals and org_vals[0].strip():
            return org_vals[0].strip()
        hdr = self.headers.get("X-SelfHeal-Org", "").strip()
        return hdr or "default"

    def do_GET(self) -> None:
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        query = parse_qs(parsed_url.query)
        if path == "/healthz":
            self._send_json(
                HTTPStatus.OK,
                {"status": "ok", "version": SERVER_VERSION},
            )
            return
        if path == "/readyz":
            if self.server.drift_ledger.ping():
                self._send_json(HTTPStatus.OK, {"status": "ready"})
            else:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE, {"status": "unavailable"}
                )
            return
        if path == "/metrics":
            fmt_vals = query.get("format")
            if fmt_vals and fmt_vals[0].strip().lower() == "prometheus":
                self._send_text(
                    HTTPStatus.OK,
                    self._format_prometheus_metrics(),
                    content_type="text/plain; version=0.0.4; charset=utf-8",
                )
                return
            self._send_json(
                HTTPStatus.OK,
                {
                    "version": SERVER_VERSION,
                    "tenants_active": len(self.server.registry.list_org_ids()),
                    "installations_active": len(
                        self.server.installation_registry.list_org_ids()
                    ),
                    "subscriptions_active": len(
                        self.server.billing_store.list_org_ids()
                    ),
                },
            )
            return
        if path == "/api/v1/events":
            org_id = self._query_org_id(query)
            if not self._authorize_tenant_read(org_id):
                self._send_json(
                    HTTPStatus.UNAUTHORIZED, {"error": "unauthorized tenant read"}
                )
                return
            events = self.server.drift_ledger.list_events(org_id=org_id, limit=50)
            self._send_json(
                HTTPStatus.OK,
                {
                    "org_id": org_id,
                    "events": [ev.model_dump(mode="json") for ev in events],
                },
            )
            return
        if path == "/api/v1/summary":
            org_id = self._query_org_id(query)
            if not self._authorize_tenant_read(org_id):
                self._send_json(
                    HTTPStatus.UNAUTHORIZED, {"error": "unauthorized tenant read"}
                )
                return
            summary = self.server.drift_ledger.get_org_summary(org_id)
            self._send_json(HTTPStatus.OK, summary.model_dump(mode="json"))
            return
        if path == "/dashboard":
            org_id = self._query_org_id(query)
            if not self._authorize_tenant_read(org_id):
                self._send_json(
                    HTTPStatus.UNAUTHORIZED, {"error": "unauthorized tenant read"}
                )
                return
            sub = self.server.billing_store.get_subscription(org_id)
            limits = self.server.billing_store.get_limits(org_id)
            summary = self.server.drift_ledger.get_org_summary(org_id)
            events = self.server.drift_ledger.list_events(org_id=org_id, limit=15)
            snap = DashboardSnapshot(
                org_id=org_id,
                plan=str(sub.plan),
                requests_per_minute_limit=limits.max_requests_per_minute,
                llm_calls_limit=limits.max_llm_calls,
                total_events=summary.total_events,
                drifted_events=summary.drifted_events,
                unique_fingerprints=summary.unique_fingerprints,
                prs_opened=summary.prs_opened,
                recent_events=[ev.model_dump(mode="json") for ev in events],
            )
            self._send_html(HTTPStatus.OK, render_dashboard_html(snap))
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/webhooks/drift":
            self._handle_drift_webhook()
            return
        if path == "/webhooks/stripe":
            self._handle_stripe_webhook_route()
            return
        if path == "/webhooks/github":
            self._handle_github_webhook_route()
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _handle_stripe_webhook_route(self) -> None:
        raw_body = self._read_body()
        if raw_body is None:
            return
        sig_header = self.headers.get("Stripe-Signature", "")
        code, payload = handle_stripe_webhook(
            self.server.config.stripe_webhook_secret,
            raw_body,
            sig_header,
            self.server.billing_store,
        )
        self._send_json(HTTPStatus(code), payload)

    def _handle_github_webhook_route(self) -> None:
        raw_body = self._read_body()
        if raw_body is None:
            return
        sig_header = self.headers.get("X-Hub-Signature-256", "")
        event_type = self.headers.get("X-GitHub-Event", "")
        code, payload = handle_github_webhook(
            self.server.config.github_webhook_secret,
            raw_body,
            sig_header,
            event_type,
            self.server.installation_registry,
        )
        self._send_json(HTTPStatus(code), payload)

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

        raw_body = self._read_body()
        if raw_body is None:
            return

        existing_tenant = self.server.registry.get_tenant(org_id_raw)
        if existing_tenant is None and not self.server.config.allow_unregistered_tenants:
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"error": f"unknown organization: {org_id_raw}"},
            )
            return

        expected_secret = (
            existing_tenant.config.webhook_secret
            if existing_tenant is not None
            else self.server.config.webhook_secret
        )
        if expected_secret:
            sig_header = (
                self.headers.get("X-Hub-Signature-256")
                or self.headers.get("X-SelfHeal-Signature")
                or ""
            )
            if not verify_webhook_signature(expected_secret, raw_body, sig_header):
                self._send_json(
                    HTTPStatus.UNAUTHORIZED,
                    {"error": "invalid or missing webhook signature"},
                )
                return

        tenant = self.server.registry.get_or_create(org_id_raw)
        if tenant is None:
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"error": f"unknown organization: {org_id_raw}"},
            )
            return

        if org_id_raw in self.server.billing_store.list_org_ids():
            limits = self.server.billing_store.get_limits(org_id_raw)
            tenant.rate_limiter.update_limit(limits.max_requests_per_minute)
            if tenant.mapping_provider is not None:
                tenant.mapping_provider.set_max_calls(limits.max_llm_calls)

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

        request_id = self._get_request_id()
        try:
            pre_report = detect_drift(
                expected_model, req.payload, mapping_provider=None
            )
            pre_fp = compute_drift_fingerprint(pre_report)

            is_new, cached_plan = tenant.deduplicator.begin(pre_fp)
            report = pre_report
            plan: PatchPlan | None = cached_plan
            if is_new:
                try:
                    if pre_report.has_drift and tenant.mapping_provider is not None:
                        report = detect_drift(
                            expected_model,
                            req.payload,
                            mapping_provider=tenant.mapping_provider,
                        )
                    plan = build_patch_plan(report, root_model=expected_model)
                    tenant.deduplicator.complete(pre_fp, plan)
                except Exception:
                    tenant.deduplicator.abort(pre_fp)
                    raise
            elif plan is None:
                plan = build_patch_plan(report, root_model=expected_model)
            fingerprint = compute_drift_fingerprint(report)

            include_zod = (
                req.include_zod_adapter
                if req.include_zod_adapter is not None
                else self.server.config.include_zod_adapter
            )
            zod_adapter: str | None = None
            if include_zod and plan.actions:
                zod_adapter = synthesize_zod_preprocessor(plan)

            issue_kinds = tuple(str(i.kind) for i in report.issues)
            self.server.drift_ledger.record_event(
                org_id=org_id_raw,
                model_name=req.model,
                fingerprint=fingerprint,
                has_drift=report.has_drift,
                issue_count=len(report.issues),
                issue_kinds=issue_kinds,
            )
            if report.has_drift and is_new and self.server.notifier is not None:
                self.server.notifier.notify(
                    DriftAlert(
                        org_id=org_id_raw,
                        model_name=req.model,
                        fingerprint=fingerprint,
                        issue_count=len(report.issues),
                        issue_kinds=issue_kinds,
                    )
                )
        except Exception:
            _logger.exception(
                "drift_webhook_failed request_id=%s org_id=%s model=%s",
                request_id,
                org_id_raw,
                req.model,
            )
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "internal server error", "request_id": request_id},
            )
            return

        _logger.info(
            "drift_webhook request_id=%s org_id=%s model=%s "
            "has_drift=%s fingerprint=%s deduplicated=%s",
            request_id,
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
    ledger = DriftLedger(db_path=resolved_config.db_path)
    return SelfHealHTTPServer(
        (host, port),
        SelfHealRequestHandler,
        config=resolved_config,
        registry=registry,
        model_registry=model_registry or {},
        drift_ledger=ledger,
    )


def _load_config_from_env_and_file(config_path: str | None) -> ServerConfig:
    if config_path:
        raw = json.loads(Path(config_path).read_text(encoding="utf-8"))
        return ServerConfig.model_validate(raw)
    port_env = int(os.environ.get("PORT", "8080"))
    host_env = os.environ.get("SELFHEAL_HOST", "0.0.0.0")
    secret_env = os.environ.get("SELFHEAL_WEBHOOK_SECRET", "")
    stripe_env = os.environ.get(
        "STRIPE_WEBHOOK_SECRET",
        os.environ.get("SELFHEAL_STRIPE_WEBHOOK_SECRET", ""),
    )
    github_env = os.environ.get(
        "GITHUB_APP_WEBHOOK_SECRET",
        os.environ.get("SELFHEAL_GITHUB_WEBHOOK_SECRET", ""),
    )
    rpm_env = int(os.environ.get("SELFHEAL_MAX_RPM", "60"))
    llm_cap_env = int(os.environ.get("SELFHEAL_MAX_LLM_CALLS", "20"))
    db_path_env = os.environ.get("SELFHEAL_DB_PATH", "").strip() or ":memory:"
    opts: dict[str, Any] = {
        "host": host_env,
        "port": port_env,
        "webhook_secret": secret_env,
        "stripe_webhook_secret": stripe_env,
        "github_webhook_secret": github_env,
        "max_requests_per_minute": rpm_env,
        "max_llm_calls_per_tenant": llm_cap_env,
        "db_path": db_path_env,
    }
    return ServerConfig(**opts)


def _load_model_registry(specs: list[str]) -> dict[str, type[BaseModel]]:
    """Import ``module.path:ClassName`` specs and validate each is a Pydantic BaseModel."""
    registry: dict[str, type[BaseModel]] = {}
    for raw_spec in specs:
        spec = raw_spec.strip()
        if not spec:
            continue
        if ":" not in spec:
            raise ValueError(
                f"Invalid model spec {spec!r}; expected 'module.path:ClassName'"
            )
        module_path, class_name = spec.split(":", 1)
        module_path = module_path.strip()
        class_name = class_name.strip()
        if not module_path or not class_name:
            raise ValueError(
                f"Invalid model spec {spec!r}; expected 'module.path:ClassName'"
            )
        module = importlib.import_module(module_path)
        attr = getattr(module, class_name, None)
        if attr is None:
            raise ValueError(f"Model {class_name!r} not found in module {module_path!r}")
        if not isinstance(attr, type) or not issubclass(attr, BaseModel):
            raise ValueError(f"Model {spec!r} is not a Pydantic BaseModel subclass")
        registry[class_name] = attr
    return registry


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
    parser.add_argument(
        "--models",
        action="append",
        default=None,
        help="Model spec(s) 'module.path:ClassName' (or comma-separated via SELFHEAL_MODELS)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = _load_config_from_env_and_file(args.config)
    host = args.host if args.host is not None else config.host
    port = args.port if args.port is not None else config.port

    raw_models: list[str] = []
    if args.models:
        for item in args.models:
            raw_models.extend(part.strip() for part in item.split(",") if part.strip())
    else:
        env_models = os.environ.get("SELFHEAL_MODELS", "")
        raw_models = [part.strip() for part in env_models.split(",") if part.strip()]
    model_registry = _load_model_registry(raw_models)

    server = create_server(
        host=host, port=port, config=config, model_registry=model_registry
    )
    _logger.info("Starting selfheal-server on %s:%d", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _logger.info("Shutting down selfheal-server")
    finally:
        if hasattr(server, "drift_ledger"):
            server.drift_ledger.close()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main_server())
