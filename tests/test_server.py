from __future__ import annotations

import hashlib
import hmac
import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from pydantic import BaseModel

from selfheal_api.server import (
    SelfHealHTTPServer,
    ServerConfig,
    TenantConfig,
    create_server,
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
