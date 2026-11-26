from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from selfheal_api.billing import PlanTier, SubscriptionState
from selfheal_api.server import (
    SelfHealHTTPServer,
    ServerConfig,
    TenantConfig,
    TenantRegistry,
    _load_config_from_env_and_file,
    _load_model_registry,
    create_server,
    main_server,
    verify_webhook_signature,
)


class OrderPayload(BaseModel):
    order_id: int
    customer_email: str
    total_cents: int


def _sign_body(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


@contextmanager
def _running_server(
    config: ServerConfig,
    model_registry: dict[str, type[BaseModel]] | None = None,
) -> Iterator[tuple[SelfHealHTTPServer, str]]:
    registry = model_registry if model_registry is not None else {"OrderPayload": OrderPayload}
    server = create_server(
        host="127.0.0.1",
        port=0,
        config=config,
        model_registry=registry,
    )
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def _http_request(
    url: str,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], dict[str, Any]]:
    req = urllib.request.Request(
        url,
        data=body,
        headers=headers or {},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, dict(resp.headers.items()), json.loads(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        return exc.code, dict(exc.headers.items()), json.loads(raw)


def test_verify_webhook_signature_unit() -> None:
    secret = "whsec_test_secret"
    body = b'{"model":"OrderPayload","payload":{}}'
    valid_sig = _sign_body(secret, body)

    assert verify_webhook_signature(secret, body, valid_sig) is True
    assert verify_webhook_signature(secret, body, valid_sig.upper()) is True
    assert verify_webhook_signature("wrong_secret", body, valid_sig) is False
    assert verify_webhook_signature(secret, body, "sha1=deadbeef") is False
    assert verify_webhook_signature("", body, valid_sig) is False
    assert verify_webhook_signature(secret, body, "") is False


def test_healthz_returns_ok() -> None:
    config = ServerConfig()
    with _running_server(config) as (_, base_url):
        status, _, data = _http_request(f"{base_url}/healthz")
        assert status == 200
        assert data == {"status": "ok", "version": "0.2.0"}


def test_valid_signed_drift_webhook_redacts_pii_and_emits_zod() -> None:
    secret = "org-a-secret"
    config = ServerConfig(
        tenants={"org-a": TenantConfig(webhook_secret=secret, max_requests_per_minute=60)},
        allow_unregistered_tenants=False,
    )
    drifted_body = json.dumps(
        {
            "model": "OrderPayload",
            "payload": {
                "order_id": "12345",
                "customer_email": "alice@example.com",
                "amount_cents": 4999,
            },
        }
    ).encode("utf-8")

    with _running_server(config) as (_, base_url):
        status, _, data = _http_request(
            f"{base_url}/webhooks/drift",
            method="POST",
            body=drifted_body,
            headers={
                "Content-Type": "application/json",
                "X-SelfHeal-Org": "org-a",
                "X-Hub-Signature-256": _sign_body(secret, drifted_body),
            },
        )
        assert status == 200
        assert data["org_id"] == "org-a"
        assert data["model"] == "OrderPayload"
        assert data["has_drift"] is True
        assert data["deduplicated"] is False
        assert isinstance(data["fingerprint"], str) and len(data["fingerprint"]) == 64
        assert len(data["issues"]) > 0
        # Ensure zero-PII: raw values never appear in serialized issues
        serialized = json.dumps(data["issues"])
        assert "alice@example.com" not in serialized
        assert "12345" not in serialized
        assert "4999" not in serialized
        assert isinstance(data["zod_adapter"], str)
        assert "createSelfHealZodAdapter" in data["zod_adapter"]


def test_invalid_or_missing_signature_returns_401() -> None:
    secret = "super-secret"
    config = ServerConfig(
        tenants={"org-a": TenantConfig(webhook_secret=secret)},
    )
    body = json.dumps(
        {
            "model": "OrderPayload",
            "payload": {
                "order_id": 1,
                "customer_email": "bob@example.com",
                "total_cents": 100,
            },
        }
    ).encode("utf-8")

    with _running_server(config) as (_, base_url):
        # Missing signature header
        status_missing, _, err_missing = _http_request(
            f"{base_url}/webhooks/drift",
            method="POST",
            body=body,
            headers={
                "Content-Type": "application/json",
                "X-SelfHeal-Org": "org-a",
            },
        )
        assert status_missing == 401
        assert "signature" in err_missing["error"].lower()

        # Wrong signature header
        status_bad, _, err_bad = _http_request(
            f"{base_url}/webhooks/drift",
            method="POST",
            body=body,
            headers={
                "Content-Type": "application/json",
                "X-SelfHeal-Org": "org-a",
                "X-SelfHeal-Signature": "sha256=" + ("0" * 64),
            },
        )
        assert status_bad == 401
        assert "signature" in err_bad["error"].lower()


def test_tenant_isolation_deduplicator_and_llm_provider() -> None:
    config = ServerConfig(
        tenants={
            "org-a": TenantConfig(
                webhook_secret="secret-a",
                enable_llm_fallback=True,
                max_llm_calls_per_tenant=5,
            ),
            "org-b": TenantConfig(
                webhook_secret="secret-b",
                enable_llm_fallback=True,
                max_llm_calls_per_tenant=10,
            ),
        },
        allow_unregistered_tenants=False,
    )
    body = json.dumps(
        {
            "model": "OrderPayload",
            "payload": {
                "order_id": "99",
                "customer_email": "c@example.com",
                "total_cents": 250,
            },
        }
    ).encode("utf-8")

    with _running_server(config) as (server, base_url):
        # First call for org-a: not deduplicated
        s1, _, d1 = _http_request(
            f"{base_url}/webhooks/drift",
            method="POST",
            body=body,
            headers={
                "Content-Type": "application/json",
                "X-SelfHeal-Org": "org-a",
                "X-Hub-Signature-256": _sign_body("secret-a", body),
            },
        )
        assert s1 == 200
        assert d1["deduplicated"] is False

        # Second call for org-a: deduplicated within org-a
        s2, _, d2 = _http_request(
            f"{base_url}/webhooks/drift",
            method="POST",
            body=body,
            headers={
                "Content-Type": "application/json",
                "X-SelfHeal-Org": "org-a",
                "X-Hub-Signature-256": _sign_body("secret-a", body),
            },
        )
        assert s2 == 200
        assert d2["deduplicated"] is True
        assert d2["fingerprint"] == d1["fingerprint"]

        # First call for org-b with identical payload: NOT deduplicated (isolated!)
        s3, _, d3 = _http_request(
            f"{base_url}/webhooks/drift",
            method="POST",
            body=body,
            headers={
                "Content-Type": "application/json",
                "X-SelfHeal-Org": "org-b",
                "X-Hub-Signature-256": _sign_body("secret-b", body),
            },
        )
        assert s3 == 200
        assert d3["deduplicated"] is False
        assert d3["fingerprint"] == d1["fingerprint"]

        tenant_a = server.registry.get_tenant("org-a")
        tenant_b = server.registry.get_tenant("org-b")
        assert tenant_a is not None and tenant_b is not None
        assert tenant_a.deduplicator is not tenant_b.deduplicator
        assert tenant_a.rate_limiter is not tenant_b.rate_limiter
        assert tenant_a.mapping_provider is not tenant_b.mapping_provider


def test_per_tenant_rate_limit_returns_429() -> None:
    config = ServerConfig(
        tenants={
            "org-a": TenantConfig(webhook_secret="sec-a", max_requests_per_minute=2),
            "org-b": TenantConfig(webhook_secret="sec-b", max_requests_per_minute=2),
        },
        allow_unregistered_tenants=False,
    )
    body = json.dumps(
        {
            "model": "OrderPayload",
            "payload": {
                "order_id": 1,
                "customer_email": "x@example.com",
                "total_cents": 100,
            },
        }
    ).encode("utf-8")

    with _running_server(config) as (_, base_url):
        headers_a = {
            "Content-Type": "application/json",
            "X-SelfHeal-Org": "org-a",
            "X-SelfHeal-Signature": _sign_body("sec-a", body),
        }
        # First 2 requests succeed
        assert _http_request(f"{base_url}/webhooks/drift", "POST", body, headers_a)[0] == 200
        assert _http_request(f"{base_url}/webhooks/drift", "POST", body, headers_a)[0] == 200

        # 3rd request for org-a is rate-limited
        status_429, resp_headers, err_data = _http_request(
            f"{base_url}/webhooks/drift", "POST", body, headers_a
        )
        assert status_429 == 429
        assert resp_headers.get("Retry-After") == "60"
        assert "rate limit" in err_data["error"].lower()

        # org-b still has its own independent token bucket
        headers_b = {
            "Content-Type": "application/json",
            "X-SelfHeal-Org": "org-b",
            "X-SelfHeal-Signature": _sign_body("sec-b", body),
        }
        status_b, _, _ = _http_request(
            f"{base_url}/webhooks/drift", "POST", body, headers_b
        )
        assert status_b == 200


def test_missing_org_header_and_unknown_org_rejected() -> None:
    config = ServerConfig(
        tenants={"org-a": TenantConfig()},
        allow_unregistered_tenants=False,
    )
    body = json.dumps({"model": "OrderPayload", "payload": {}}).encode("utf-8")

    with _running_server(config) as (_, base_url):
        s_missing, _, _ = _http_request(
            f"{base_url}/webhooks/drift",
            method="POST",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        assert s_missing == 400

        s_unknown, _, _ = _http_request(
            f"{base_url}/webhooks/drift",
            method="POST",
            body=body,
            headers={"Content-Type": "application/json", "X-SelfHeal-Org": "org-unknown"},
        )
        assert s_unknown == 403


def test_main_server_help_exits_zero() -> None:
    from selfheal_api.server import main_server

    with pytest.raises(SystemExit) as exc_info:
        main_server(["--help"])
    assert exc_info.value.code == 0


def test_server_stripe_github_and_metrics_routes() -> None:
    import time

    s_val = "s1"
    g_val = "g1"
    cfg_opts: dict[str, Any] = {
        "stripe_webhook_secret": s_val,
        "github_webhook_secret": g_val,
    }
    cfg = ServerConfig(**cfg_opts)
    with _running_server(cfg) as (server, base_url):
        ts = int(time.time())
        stripe_body = json.dumps(
            {
                "id": "evt_srv_1",
                "type": "checkout.session.completed",
                "data": {
                    "object": {
                        "client_reference_id": "org-stripe",
                        "metadata": {"org_id": "org-stripe", "plan": "pro"},
                    }
                },
            }
        ).encode("utf-8")
        stripe_sig = hmac.new(
            s_val.encode("utf-8"),
            f"{ts}.".encode() + stripe_body,
            hashlib.sha256,
        ).hexdigest()
        s_status, _, s_json = _http_request(
            f"{base_url}/webhooks/stripe",
            method="POST",
            body=stripe_body,
            headers={"Stripe-Signature": f"t={ts},v1={stripe_sig}"},
        )
        assert s_status == 200
        assert s_json["plan"] == "pro"

        gh_body = json.dumps(
            {
                "action": "created",
                "installation": {
                    "id": 9001,
                    "account": {"login": "creova-gif"},
                },
                "repositories": [{"full_name": "creova-gif/selfheal-api"}],
            }
        ).encode("utf-8")
        gh_sig = (
            "sha256="
            + hmac.new(
                g_val.encode("utf-8"), gh_body, hashlib.sha256
            ).hexdigest()
        )
        g_status, _, g_json = _http_request(
            f"{base_url}/webhooks/github",
            method="POST",
            body=gh_body,
            headers={
                "X-Hub-Signature-256": gh_sig,
                "X-GitHub-Event": "installation",
            },
        )
        assert g_status == 200
        assert g_json["installation_id"] == 9001

        m_status, _, m_json = _http_request(f"{base_url}/metrics")
        assert m_status == 200
        assert m_json["subscriptions_active"] == 1
        assert m_json["installations_active"] == 1
        assert server.billing_store.get_subscription("org-stripe").plan == "pro"


def test_server_ledger_notifier_api_and_dashboard() -> None:
    from selfheal_api.notifier import MockAlertNotifier

    notifier = MockAlertNotifier()
    with _running_server(ServerConfig()) as (server, base_url):
        server.notifier = notifier
        body = json.dumps(
            {"model": "OrderPayload", "payload": {"id": "o1", "amount_cents": 500}}
        ).encode("utf-8")
        status, _, resp = _http_request(
            f"{base_url}/webhooks/drift",
            method="POST",
            body=body,
            headers={"X-SelfHeal-Org": "org-dash"},
        )
        assert status == 200
        assert resp["has_drift"] is True
        assert len(notifier.alerts) == 1
        assert notifier.alerts[0].org_id == "org-dash"

        ev_status, _, ev_json = _http_request(
            f"{base_url}/api/v1/events?org_id=org-dash"
        )
        assert ev_status == 200
        assert len(ev_json["events"]) == 1
        assert ev_json["events"][0]["model_name"] == "OrderPayload"

        sum_status, _, sum_json = _http_request(
            f"{base_url}/api/v1/summary?org_id=org-dash"
        )
        assert sum_status == 200
        assert sum_json["drifted_events"] == 1

        with urllib.request.urlopen(f"{base_url}/dashboard?org_id=org-dash") as d_resp:
            html_text = d_resp.read().decode("utf-8")
            assert d_resp.status == 200
            assert "SelfHealAPI Drift Console" in html_text
            assert "org-dash" in html_text
            assert "OrderPayload" in html_text
            assert "DRIFTED" in html_text


def test_tenant_read_endpoints_require_auth_and_error_codes() -> None:
    cfg_opts: dict[str, Any] = {
        "tenants": {
            "org-a": TenantConfig.model_validate({"webhook_secret": "s-auth-a"}),
        },
        "allow_unregistered_tenants": False,
        "max_body_bytes": 1024,
    }
    config = ServerConfig(**cfg_opts)
    with _running_server(config) as (_, base_url):
        for path in (
            "/api/v1/events?org_id=org-a",
            "/api/v1/summary?org_id=org-a",
            "/dashboard?org_id=org-a",
        ):
            status, _, _ = _http_request(f"{base_url}{path}")
            assert status == 401

        auth_status, _, _ = _http_request(
            f"{base_url}/api/v1/summary?org_id=org-a",
            headers={"Authorization": "Bearer s-auth-a"},
        )
        assert auth_status == 200

        oversized = b'{"model":"OrderPayload","payload":{"x":"' + (b"a" * 2048) + b'"}}'
        os_sig = (
            "sha256="
            + hmac.new(b"s-auth-a", oversized, hashlib.sha256).hexdigest()
        )
        ov_status, _, _ = _http_request(
            f"{base_url}/webhooks/drift",
            method="POST",
            body=oversized,
            headers={"X-SelfHeal-Org": "org-a", "X-Hub-Signature-256": os_sig},
        )
        assert ov_status == 413


def test_load_model_registry_and_selfheal_models_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _load_model_registry(["selfheal_api.server:ServerConfig", "  "])
    assert registry == {"ServerConfig": ServerConfig}

    with pytest.raises(ValueError, match="Invalid model spec"):
        _load_model_registry(["invalid_spec_without_colon"])

    with pytest.raises(ValueError, match="not found"):
        _load_model_registry(["selfheal_api.server:DoesNotExist"])

    with pytest.raises(ValueError, match="not a Pydantic BaseModel"):
        _load_model_registry(["selfheal_api.server:TenantRegistry"])

    captured: dict[str, Any] = {}

    class _DummyServer:
        def serve_forever(self) -> None:
            return None

        def server_close(self) -> None:
            captured["closed"] = True

    def _fake_create_server(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _DummyServer()

    monkeypatch.setenv(
        "SELFHEAL_MODELS",
        "selfheal_api.server:ServerConfig, selfheal_api.server:TenantConfig",
    )
    monkeypatch.setattr("selfheal_api.server.create_server", _fake_create_server)

    exit_code = main_server(["--host", "127.0.0.1", "--port", "0"])
    assert exit_code == 0
    assert captured["model_registry"] == {
        "ServerConfig": ServerConfig,
        "TenantConfig": TenantConfig,
    }
    assert captured["closed"] is True

    # --models CLI flag overrides SELFHEAL_MODELS
    captured.clear()
    exit_code_cli = main_server(
        ["--host", "127.0.0.1", "--port", "0", "--models", "selfheal_api.server:ServerConfig"]
    )
    assert exit_code_cli == 0
    assert captured["model_registry"] == {"ServerConfig": ServerConfig}


def test_webhook_consults_billing_store_limits() -> None:
    config = ServerConfig(
        tenants={
            "org-free": TenantConfig(
                max_requests_per_minute=100,
                max_llm_calls_per_tenant=50,
                enable_llm_fallback=True,
            ),
        },
        allow_unregistered_tenants=False,
    )
    body = json.dumps(
        {
            "model": "OrderPayload",
            "payload": {
                "order_id": 1,
                "customer_email": "alice@example.com",
                "total_cents": 100,
            },
        }
    ).encode("utf-8")

    with _running_server(config) as (server, base_url):
        server.billing_store.upsert_subscription(
            SubscriptionState(org_id="org-free", plan=PlanTier.FREE, status="active")
        )
        status, _, _ = _http_request(
            f"{base_url}/webhooks/drift",
            method="POST",
            body=body,
            headers={
                "Content-Type": "application/json",
                "X-SelfHeal-Org": "org-free",
            },
        )
        assert status == 200
        tenant = server.registry.get_tenant("org-free")
        assert tenant is not None
        assert tenant.rate_limiter._capacity == 30.0
        assert tenant.mapping_provider is not None
        assert tenant.mapping_provider._max_calls == 10


def test_readyz_returns_ready_and_unavailable_when_closed() -> None:
    config = ServerConfig()
    with _running_server(config) as (server, base_url):
        status, headers, data = _http_request(f"{base_url}/readyz")
        assert status == 200
        assert data == {"status": "ready"}
        assert "X-Request-Id" in headers

        server.drift_ledger.close()
        status_down, _, data_down = _http_request(f"{base_url}/readyz")
        assert status_down == 503
        assert data_down == {"status": "unavailable"}


def test_request_id_correlation_propagation_and_generation() -> None:
    config = ServerConfig()
    with _running_server(config) as (_, base_url):
        # Valid alphanumeric/hyphen <=64 chars is propagated on JSON
        status, headers, _ = _http_request(
            f"{base_url}/healthz",
            headers={"X-Request-Id": "req-trace-001"},
        )
        assert status == 200
        assert headers.get("X-Request-Id") == "req-trace-001"

        # Valid X-Request-Id propagated on HTML dashboard
        d_req = urllib.request.Request(
            f"{base_url}/dashboard?org_id=org-corr",
            headers={"X-Request-Id": "dash-req-99"},
        )
        with urllib.request.urlopen(d_req, timeout=5.0) as d_resp:
            assert d_resp.status == 200
            assert d_resp.headers.get("X-Request-Id") == "dash-req-99"

        # Invalid or oversized X-Request-Id is replaced by 16-char hex uuid
        for bad_id in ("bad_underscore", "a" * 65, ""):
            _, bad_headers, _ = _http_request(
                f"{base_url}/healthz",
                headers={"X-Request-Id": bad_id} if bad_id else {},
            )
            generated = bad_headers.get("X-Request-Id", "")
            assert len(generated) == 16
            assert all(c in "0123456789abcdef" for c in generated)


def test_metrics_prometheus_format() -> None:
    config = ServerConfig(
        tenants={"org-prom": TenantConfig()},
        allow_unregistered_tenants=True,
    )
    body = json.dumps(
        {
            "model": "OrderPayload",
            "payload": {
                "order_id": "not-an-int",
                "customer_email": "bob@example.com",
                "total_cents": 200,
            },
        }
    ).encode("utf-8")

    with _running_server(config) as (server, base_url):
        server.billing_store.upsert_subscription(
            SubscriptionState(org_id="org-prom", plan=PlanTier.PRO, status="active")
        )
        post_status, _, _ = _http_request(
            f"{base_url}/webhooks/drift",
            method="POST",
            body=body,
            headers={
                "Content-Type": "application/json",
                "X-SelfHeal-Org": "org-prom",
            },
        )
        assert post_status == 200

        req = urllib.request.Request(
            f"{base_url}/metrics?format=prometheus",
            headers={"X-Request-Id": "prom-123"},
        )
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            text = resp.read().decode("utf-8")
            assert resp.status == 200
            assert (
                resp.headers.get("Content-Type")
                == "text/plain; version=0.0.4; charset=utf-8"
            )
            assert resp.headers.get("X-Request-Id") == "prom-123"
            assert "selfheal_tenants_active 1" in text
            assert "selfheal_installations_active 0" in text
            assert "selfheal_subscriptions_active 1" in text
            assert "selfheal_drift_events_total 1" in text
            assert "selfheal_drifted_events_total 1" in text
            assert "selfheal_prs_opened_total 0" in text


def test_selfheal_db_path_persistence_and_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_file = tmp_path / "persisted" / "ledger.db"
    monkeypatch.setenv("SELFHEAL_DB_PATH", str(db_file))
    cfg = _load_config_from_env_and_file(None)
    assert cfg.db_path == str(db_file)

    body = json.dumps(
        {
            "model": "OrderPayload",
            "payload": {
                "order_id": "bad",
                "customer_email": "alice@example.com",
                "total_cents": 500,
            },
        }
    ).encode("utf-8")

    with _running_server(cfg) as (server1, base_url1):
        status, _, _ = _http_request(
            f"{base_url1}/webhooks/drift",
            method="POST",
            body=body,
            headers={
                "Content-Type": "application/json",
                "X-SelfHeal-Org": "org-persist",
            },
        )
        assert status == 200
        server1.drift_ledger.close()

    with _running_server(cfg) as (server2, base_url2):
        ev_status, _, ev_data = _http_request(
            f"{base_url2}/api/v1/events?org_id=org-persist"
        )
        assert ev_status == 200
        assert len(ev_data["events"]) == 1
        assert ev_data["events"][0]["model_name"] == "OrderPayload"
        server2.drift_ledger.close()


def test_drift_webhook_internal_error_returns_500_with_request_id(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated synthesis crash")

    monkeypatch.setattr("selfheal_api.server.detect_drift", _boom)

    config = ServerConfig()
    body = json.dumps(
        {
            "model": "OrderPayload",
            "payload": {
                "order_id": 1,
                "customer_email": "alice@example.com",
                "total_cents": 100,
            },
        }
    ).encode("utf-8")

    with caplog.at_level("ERROR", logger="selfheal_api.server"):
        with _running_server(config) as (_, base_url):
            status, headers, data = _http_request(
                f"{base_url}/webhooks/drift",
                method="POST",
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "X-SelfHeal-Org": "org-err",
                    "X-Request-Id": "req-err-500",
                },
            )
            assert status == 500
            assert headers.get("X-Request-Id") == "req-err-500"
            assert data == {
                "error": "internal server error",
                "request_id": "req-err-500",
            }
            assert any("req-err-500" in rec.message for rec in caplog.records)


def test_server_config_model_specs_loaded_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = ServerConfig.model_validate(
        {"model_specs": "selfheal_api.server:ServerConfig, selfheal_api.server:TenantConfig"}
    )
    assert cfg.model_specs == [
        "selfheal_api.server:ServerConfig",
        "selfheal_api.server:TenantConfig",
    ]
    srv = create_server(host="127.0.0.1", port=0, config=cfg)
    try:
        assert srv.model_registry == {
            "ServerConfig": ServerConfig,
            "TenantConfig": TenantConfig,
        }
    finally:
        srv.drift_ledger.close()
        srv.server_close()

    cfg_file = tmp_path / "server.json"
    cfg_file.write_text(
        json.dumps({"model_specs": ["selfheal_api.server:ServerConfig"]}),
        encoding="utf-8",
    )
    monkeypatch.delenv("SELFHEAL_MODELS", raising=False)

    captured: dict[str, Any] = {}

    class _DummyServer:
        def serve_forever(self) -> None:
            return None

        def server_close(self) -> None:
            captured["closed"] = True

    def _fake_create_server(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _DummyServer()

    monkeypatch.setattr("selfheal_api.server.create_server", _fake_create_server)
    assert main_server(["--config", str(cfg_file)]) == 0
    assert captured["model_registry"] == {"ServerConfig": ServerConfig}


def test_multitenant_read_isolation_blocks_shared_secret_fallback() -> None:
    config = ServerConfig(
        webhook_secret="shared-server-secret",
        api_secret="shared-api-secret",
        tenants={
            "org-a": TenantConfig(api_secret="tenant-a-api-secret"),
            "org-b": TenantConfig(webhook_secret="tenant-b-wh-secret"),
        },
        allow_unregistered_tenants=True,
    )
    with _running_server(config) as (_, base_url):
        # org-a rejects shared server secrets and org-b's secret
        for bad_secret in ("shared-server-secret", "shared-api-secret", "tenant-b-wh-secret"):
            for path in (
                "/api/v1/events?org_id=org-a",
                "/api/v1/summary?org_id=org-a",
                "/dashboard?org_id=org-a",
            ):
                status, _, _ = _http_request(
                    f"{base_url}{path}",
                    headers={"Authorization": f"Bearer {bad_secret}"},
                )
                assert status == 401

        # org-a accepts ONLY its own api_secret
        ok_status, _, _ = _http_request(
            f"{base_url}/api/v1/events?org_id=org-a",
            headers={"Authorization": "Bearer tenant-a-api-secret"},
        )
        assert ok_status == 200

        # Unknown tenant in multi-tenant mode rejects shared server secret
        unk_status, _, _ = _http_request(
            f"{base_url}/api/v1/summary?org_id=org-unknown",
            headers={"Authorization": "Bearer shared-api-secret"},
        )
        assert unk_status == 401

    # Explicit allow_admin_read_bypass=True permits server_secret for unknown tenants
    bypass_cfg = config.model_copy(update={"allow_admin_read_bypass": True})
    with _running_server(bypass_cfg) as (_, base_url):
        bypass_status, _, _ = _http_request(
            f"{base_url}/api/v1/summary?org_id=org-unknown",
            headers={"Authorization": "Bearer shared-api-secret"},
        )
        assert bypass_status == 200


def test_tenant_registry_pinned_tenants_never_evicted() -> None:
    cfg = ServerConfig(
        tenants={"org-pinned": TenantConfig(webhook_secret="pinned-secret")},
        allow_unregistered_tenants=True,
    )
    registry = TenantRegistry(cfg, max_tenants=3)

    pinned = registry.get_tenant("org-pinned")
    assert pinned is not None
    assert pinned.pinned is True

    # Admit 3 dynamic tenants into remaining capacity of 2 -> dyn-1 evicted, pinned kept
    assert registry.get_or_create("dyn-1") is not None
    assert registry.get_or_create("dyn-2") is not None
    assert registry.get_or_create("dyn-3") is not None

    assert registry.get_tenant("dyn-1") is None
    assert registry.get_tenant("dyn-2") is not None
    assert registry.get_tenant("dyn-3") is not None
    still_pinned = registry.get_tenant("org-pinned")
    assert still_pinned is pinned
    assert still_pinned.config.webhook_secret == "pinned-secret"

    # When all capacity is pinned, new dynamic tenants are rejected cleanly
    full_registry = TenantRegistry(cfg, max_tenants=1)
    assert full_registry.get_or_create("dyn-rejected") is None
    assert full_registry.get_tenant("org-pinned") is not None


def test_paid_upgrade_via_stripe_immediately_updates_tenant_limits() -> None:
    import time

    stripe_secret = "whsec_upgrade"
    cfg = ServerConfig(
        stripe_webhook_secret=stripe_secret,
        tenants={
            "org-up": TenantConfig(
                max_requests_per_minute=2,
                max_llm_calls_per_tenant=5,
                enable_llm_fallback=True,
            ),
        },
        allow_unregistered_tenants=False,
    )
    drift_body = json.dumps(
        {
            "model": "OrderPayload",
            "payload": {
                "order_id": 1,
                "customer_email": "alice@example.com",
                "total_cents": 100,
            },
        }
    ).encode("utf-8")

    with _running_server(cfg) as (server, base_url):
        headers = {"Content-Type": "application/json", "X-SelfHeal-Org": "org-up"}
        assert _http_request(f"{base_url}/webhooks/drift", "POST", drift_body, headers)[0] == 200
        assert _http_request(f"{base_url}/webhooks/drift", "POST", drift_body, headers)[0] == 200
        assert _http_request(f"{base_url}/webhooks/drift", "POST", drift_body, headers)[0] == 429

        ts = int(time.time())
        stripe_body = json.dumps(
            {
                "id": "evt_upgrade_1",
                "type": "checkout.session.completed",
                "created": ts,
                "data": {
                    "object": {
                        "customer": "cus_up",
                        "subscription": "sub_up",
                        "metadata": {"org_id": "org-up", "plan": "pro"},
                    }
                },
            }
        ).encode("utf-8")
        stripe_sig = hmac.new(
            stripe_secret.encode("utf-8"),
            f"{ts}.".encode() + stripe_body,
            hashlib.sha256,
        ).hexdigest()
        s_status, _, _ = _http_request(
            f"{base_url}/webhooks/stripe",
            method="POST",
            body=stripe_body,
            headers={"Stripe-Signature": f"t={ts},v1={stripe_sig}"},
        )
        assert s_status == 200

        # Immediately after Stripe upgrade, tenant limits reflect PRO (120 rpm, 200 LLM calls)
        tenant = server.registry.get_tenant("org-up")
        assert tenant is not None
        assert tenant.rate_limiter._capacity == 120.0
        assert tenant.mapping_provider is not None
        assert tenant.mapping_provider._max_calls == 200

        post_upgrade_status, _, _ = _http_request(
            f"{base_url}/webhooks/drift", "POST", drift_body, headers
        )
        assert post_upgrade_status == 200


def test_security_headers_on_json_and_html_responses() -> None:
    with _running_server(ServerConfig()) as (_server, base_url):
        status, headers, _ = _http_request(f"{base_url}/healthz")
        lower_h = {k.lower(): v for k, v in headers.items()}
        assert status == 200
        assert lower_h.get("x-content-type-options") == "nosniff"
        assert lower_h.get("cache-control") == "no-store"

        with urllib.request.urlopen(
            f"{base_url}/dashboard?org_id=acme", timeout=5.0
        ) as resp:
            d_status = resp.status
            lower_dh = {k.lower(): v for k, v in resp.headers.items()}
        assert d_status == 200
        assert lower_dh.get("x-content-type-options") == "nosniff"
        assert lower_dh.get("x-frame-options") == "DENY"
        assert "default-src 'none'" in (lower_dh.get("content-security-policy") or "")


def test_control_plane_sqlite_restart_persistence(tmp_path: Path) -> None:
    db_path = tmp_path / "control_plane.db"
    cfg = ServerConfig(
        db_path=str(db_path),
        stripe_webhook_secret="whsec_srv",
        github_webhook_secret="ghsec_srv",
    )

    ts = int(time.time())
    stripe_body = json.dumps(
        {
            "id": "evt_srv_persist",
            "type": "checkout.session.completed",
            "created": ts,
            "data": {
                "object": {
                    "customer": "cus_srv",
                    "subscription": "sub_srv",
                    "metadata": {"org_id": "org-srv-persist", "plan": "team"},
                }
            },
        }
    ).encode("utf-8")
    stripe_sig = hmac.new(
        b"whsec_srv",
        f"{ts}.".encode() + stripe_body,
        hashlib.sha256,
    ).hexdigest()

    gh_body = json.dumps(
        {
            "action": "created",
            "installation": {
                "id": 777,
                "account": {"login": "org-srv-persist"},
                "suspended_at": None,
            },
            "repositories": [{"full_name": "org-srv-persist/api"}],
        }
    ).encode("utf-8")
    gh_sig = (
        "sha256="
        + hmac.new(b"ghsec_srv", gh_body, hashlib.sha256).hexdigest()
    )

    with _running_server(cfg) as (server1, base_url1):
        s_status, _, _ = _http_request(
            f"{base_url1}/webhooks/stripe",
            method="POST",
            body=stripe_body,
            headers={"Stripe-Signature": f"t={ts},v1={stripe_sig}"},
        )
        assert s_status == 200

        g_status, _, _ = _http_request(
            f"{base_url1}/webhooks/github",
            method="POST",
            body=gh_body,
            headers={
                "X-Hub-Signature-256": gh_sig,
                "X-GitHub-Event": "installation",
            },
        )
        assert g_status == 200

        server1.drift_ledger.close()
        server1.billing_store.close()
        server1.installation_registry.close()

    with _running_server(cfg) as (server2, _base_url2):
        sub = server2.billing_store.get_subscription("org-srv-persist")
        assert sub.plan == PlanTier.TEAM
        assert sub.stripe_customer_id == "cus_srv"
        assert sub.stripe_subscription_id == "sub_srv"

        gh_rec = server2.installation_registry.get_by_org("org-srv-persist")
        assert gh_rec is not None
        assert gh_rec.installation_id == 777
        assert gh_rec.repositories == ("org-srv-persist/api",)

        server2.drift_ledger.close()
        server2.billing_store.close()
        server2.installation_registry.close()
