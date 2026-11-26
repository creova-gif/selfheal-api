from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest
from pydantic import ValidationError

from selfheal_api.billing import (
    PLAN_CATALOG,
    BillingStore,
    PlanLimits,
    PlanTier,
    SubscriptionState,
    handle_stripe_webhook,
    verify_stripe_signature,
)


def _sign_stripe_payload(secret: str, payload: bytes, ts: int) -> str:
    signed = f"{ts}.".encode() + payload
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


def test_plan_catalog_and_frozen_limits() -> None:
    assert PLAN_CATALOG[PlanTier.FREE] == PlanLimits(
        max_requests_per_minute=30,
        max_llm_calls=10,
        max_models=3,
    )
    assert PLAN_CATALOG[PlanTier.PRO] == PlanLimits(
        max_requests_per_minute=120,
        max_llm_calls=200,
        max_models=25,
    )
    assert PLAN_CATALOG[PlanTier.TEAM] == PlanLimits(
        max_requests_per_minute=600,
        max_llm_calls=2000,
        max_models=200,
    )
    with pytest.raises(ValidationError):
        PLAN_CATALOG[PlanTier.FREE].max_models = 999


def test_verify_stripe_signature_valid() -> None:
    secret = "whsec_test_secret"
    payload = b'{"id":"evt_1","type":"checkout.session.completed"}'
    now = 1_700_000_000
    sig_header = _sign_stripe_payload(secret, payload, now - 60)

    assert verify_stripe_signature(secret, payload, sig_header, now=now) is True


def test_verify_stripe_signature_multiple_v1_signatures() -> None:
    secret = "whsec_test_secret"
    payload = b'{"id":"evt_multi"}'
    now = 1_700_000_000
    valid_sig = _sign_stripe_payload(secret, payload, now).split(",v1=")[1]
    sig_header = f"t={now},v1=deadbeef,v1={valid_sig}"

    assert verify_stripe_signature(secret, payload, sig_header, now=now) is True


def test_verify_stripe_signature_expired_timestamp() -> None:
    secret = "whsec_test_secret"
    payload = b'{"id":"evt_old"}'
    now = 1_700_000_000
    sig_header = _sign_stripe_payload(secret, payload, now - 301)

    assert verify_stripe_signature(secret, payload, sig_header, tolerance_s=300, now=now) is False


def test_verify_stripe_signature_tampered_body() -> None:
    secret = "whsec_test_secret"
    payload = b'{"id":"evt_1","amount":100}'
    now = 1_700_000_000
    sig_header = _sign_stripe_payload(secret, payload, now)

    tampered = b'{"id":"evt_1","amount":999}'
    assert verify_stripe_signature(secret, tampered, sig_header, now=now) is False


@pytest.mark.parametrize(
    "header",
    [
        "",
        "v1=abcdef",
        "t=notanint,v1=abcdef",
        "t=1700000000",
        "t=1700000000,v1=",
        "malformed-header",
    ],
)
def test_verify_stripe_signature_malformed_header(header: str) -> None:
    assert (
        verify_stripe_signature(
            "whsec_test_secret",
            b"{}",
            header,
            now=1_700_000_000,
        )
        is False
    )


def test_billing_store_idempotency_and_bounded_eviction() -> None:
    store = BillingStore(max_events=2)
    assert store.mark_event_processed("evt_1") is True
    assert store.mark_event_processed("evt_1") is False
    assert store.is_event_processed("evt_1") is True

    assert store.mark_event_processed("evt_2") is True
    assert store.mark_event_processed("evt_3") is True
    # evt_1 evicted when evt_3 pushed size past max_events=2
    assert store.is_event_processed("evt_1") is False
    assert store.is_event_processed("evt_2") is True
    assert store.is_event_processed("evt_3") is True

    default_sub = store.get_subscription("unknown-org")
    assert default_sub == SubscriptionState(org_id="unknown-org", plan=PlanTier.FREE)
    assert store.get_limits("unknown-org") == PLAN_CATALOG[PlanTier.FREE]


def test_handle_stripe_webhook_invalid_signature_returns_401() -> None:
    store = BillingStore()
    body = b'{"id":"evt_1","type":"checkout.session.completed"}'
    status, resp = handle_stripe_webhook(
        "whsec_test",
        body,
        "t=1700000000,v1=bad",
        store,
        now=1_700_000_000,
    )
    assert status == 401
    assert resp == {"error": "invalid_signature"}


def test_handle_stripe_webhook_invalid_json_returns_400() -> None:
    secret = "whsec_test"
    store = BillingStore()
    now = 1_700_000_000
    body = b"{not-json"
    sig = _sign_stripe_payload(secret, body, now)

    status, resp = handle_stripe_webhook(secret, body, sig, store, now=now)
    assert status == 400
    assert resp == {"error": "invalid_json"}


def test_handle_stripe_webhook_upgrade_pro_team_idempotency_and_delete() -> None:
    secret = "whsec_test"
    store = BillingStore()
    now = 1_700_000_000

    # 1. checkout.session.completed -> upgrade to PRO
    checkout_event: dict[str, Any] = {
        "id": "evt_checkout_1",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "customer": "cus_123",
                "subscription": "sub_123",
                "metadata": {"org_id": "acme", "plan": "pro"},
            }
        },
    }
    body1 = json.dumps(checkout_event).encode("utf-8")
    sig1 = _sign_stripe_payload(secret, body1, now)
    status1, resp1 = handle_stripe_webhook(secret, body1, sig1, store, now=now)
    assert status1 == 200
    assert resp1["received"] is True
    assert resp1["plan"] == "pro"

    sub = store.get_subscription("acme")
    assert sub.plan == PlanTier.PRO
    assert sub.status == "active"
    assert sub.stripe_customer_id == "cus_123"
    assert sub.stripe_subscription_id == "sub_123"
    assert store.get_limits("acme") == PLAN_CATALOG[PlanTier.PRO]

    # 2. Duplicate event_id -> idempotent 200 without re-processing
    status_dup, resp_dup = handle_stripe_webhook(secret, body1, sig1, store, now=now)
    assert status_dup == 200
    assert resp_dup == {
        "received": True,
        "duplicate": True,
        "event_id": "evt_checkout_1",
    }

    # 3. customer.subscription.updated -> upgrade to TEAM (using stripe ID fallback)
    update_event: dict[str, Any] = {
        "id": "evt_sub_update_1",
        "type": "customer.subscription.updated",
        "data": {
            "object": {
                "id": "sub_123",
                "customer": "cus_123",
                "status": "active",
                "items": {"data": [{"price": {"lookup_key": "team"}}]},
            }
        },
    }
    body2 = json.dumps(update_event).encode("utf-8")
    sig2 = _sign_stripe_payload(secret, body2, now)
    status2, resp2 = handle_stripe_webhook(secret, body2, sig2, store, now=now)
    assert status2 == 200
    assert resp2["plan"] == "team"
    assert store.get_subscription("acme").plan == PlanTier.TEAM
    assert store.get_limits("acme") == PLAN_CATALOG[PlanTier.TEAM]

    # 4. customer.subscription.deleted -> downgrade to FREE / canceled
    delete_event: dict[str, Any] = {
        "id": "evt_sub_delete_1",
        "type": "customer.subscription.deleted",
        "data": {
            "object": {
                "id": "sub_123",
                "customer": "cus_123",
            }
        },
    }
    body3 = json.dumps(delete_event).encode("utf-8")
    sig3 = _sign_stripe_payload(secret, body3, now)
    status3, resp3 = handle_stripe_webhook(secret, body3, sig3, store, now=now)
    assert status3 == 200
    assert resp3["plan"] == "free"
    assert resp3["status"] == "canceled"

    canceled_sub = store.get_subscription("acme")
    assert canceled_sub.plan == PlanTier.FREE
    assert canceled_sub.status == "canceled"
    assert store.get_limits("acme") == PLAN_CATALOG[PlanTier.FREE]


def test_late_subscription_updated_does_not_resurrect_canceled_subscription() -> None:
    secret = "whsec_test"
    store = BillingStore()
    now = 1_700_000_000

    checkout_body = json.dumps(
        {
            "id": "evt_co_1",
            "type": "checkout.session.completed",
            "created": 100,
            "data": {
                "object": {
                    "customer": "cus_late",
                    "subscription": "sub_late",
                    "metadata": {"org_id": "org-late", "plan": "pro"},
                }
            },
        }
    ).encode("utf-8")
    s1, _ = handle_stripe_webhook(
        secret, checkout_body, _sign_stripe_payload(secret, checkout_body, now), store, now=now
    )
    assert s1 == 200

    delete_body = json.dumps(
        {
            "id": "evt_del_1",
            "type": "customer.subscription.deleted",
            "created": 300,
            "data": {
                "object": {
                    "id": "sub_late",
                    "customer": "cus_late",
                }
            },
        }
    ).encode("utf-8")
    s2, _ = handle_stripe_webhook(
        secret, delete_body, _sign_stripe_payload(secret, delete_body, now), store, now=now
    )
    assert s2 == 200
    assert store.get_subscription("org-late").status == "canceled"
    assert store.get_subscription("org-late").plan == PlanTier.FREE

    # Stale out-of-order subscription.updated (created=200 < 300) must be ignored
    stale_update_body = json.dumps(
        {
            "id": "evt_upd_stale",
            "type": "customer.subscription.updated",
            "created": 200,
            "data": {
                "object": {
                    "id": "sub_late",
                    "customer": "cus_late",
                    "status": "active",
                    "items": {"data": [{"price": {"lookup_key": "team"}}]},
                }
            },
        }
    ).encode("utf-8")
    s3, resp3 = handle_stripe_webhook(
        secret,
        stale_update_body,
        _sign_stripe_payload(secret, stale_update_body, now),
        store,
        now=now,
    )
    assert s3 == 200
    assert resp3["ignored"] is True
    assert resp3["stale"] is True

    final_sub = store.get_subscription("org-late")
    assert final_sub.status == "canceled"
    assert final_sub.plan == PlanTier.FREE
    assert store.get_limits("org-late") == PLAN_CATALOG[PlanTier.FREE]


def test_stripe_retry_applies_update_after_org_id_is_linked() -> None:
    secret = "whsec_test"
    store = BillingStore()
    now = 1_700_000_000

    # 1. subscription.updated arrives before checkout links cus_retry/sub_retry -> retryable 400
    early_update_body = json.dumps(
        {
            "id": "evt_early_upd",
            "type": "customer.subscription.updated",
            "created": 200,
            "data": {
                "object": {
                    "id": "sub_retry",
                    "customer": "cus_retry",
                    "status": "active",
                    "items": {"data": [{"price": {"lookup_key": "team"}}]},
                }
            },
        }
    ).encode("utf-8")
    early_sig = _sign_stripe_payload(secret, early_update_body, now)
    s1, resp1 = handle_stripe_webhook(secret, early_update_body, early_sig, store, now=now)
    assert s1 == 400
    assert resp1["error"] == "missing_org_id"
    assert store.is_event_processed("evt_early_upd") is False

    # 2. checkout.session.completed links customer/subscription to org-retry
    checkout_body = json.dumps(
        {
            "id": "evt_checkout_link",
            "type": "checkout.session.completed",
            "created": 100,
            "data": {
                "object": {
                    "customer": "cus_retry",
                    "subscription": "sub_retry",
                    "metadata": {"org_id": "org-retry", "plan": "pro"},
                }
            },
        }
    ).encode("utf-8")
    s2, _ = handle_stripe_webhook(
        secret, checkout_body, _sign_stripe_payload(secret, checkout_body, now), store, now=now
    )
    assert s2 == 200
    assert store.get_subscription("org-retry").plan == PlanTier.PRO

    # 3. Stripe retries the exact same evt_early_upd -> now resolves org_id and applies TEAM
    s3, resp3 = handle_stripe_webhook(secret, early_update_body, early_sig, store, now=now)
    assert s3 == 200
    assert resp3.get("duplicate") is not True
    assert resp3["org_id"] == "org-retry"
    assert resp3["plan"] == "team"
    assert store.get_subscription("org-retry").plan == PlanTier.TEAM
    assert store.is_event_processed("evt_early_upd") is True
