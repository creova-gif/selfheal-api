from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from collections import OrderedDict
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict


class PlanTier(StrEnum):
    FREE = "free"
    PRO = "pro"
    TEAM = "team"


class PlanLimits(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_requests_per_minute: int
    max_llm_calls: int
    max_models: int


PLAN_CATALOG: dict[PlanTier, PlanLimits] = {
    PlanTier.FREE: PlanLimits(
        max_requests_per_minute=30,
        max_llm_calls=10,
        max_models=3,
    ),
    PlanTier.PRO: PlanLimits(
        max_requests_per_minute=120,
        max_llm_calls=200,
        max_models=25,
    ),
    PlanTier.TEAM: PlanLimits(
        max_requests_per_minute=600,
        max_llm_calls=2000,
        max_models=200,
    ),
}


class SubscriptionState(BaseModel):
    org_id: str
    plan: PlanTier = PlanTier.FREE
    status: str = "active"
    stripe_customer_id: str | None = None
    stripe_subscription_id: str | None = None


def verify_stripe_signature(
    secret: str,
    payload: bytes,
    sig_header: str,
    *,
    tolerance_s: int = 300,
    now: int | None = None,
) -> bool:
    """Verify Stripe `Stripe-Signature` header (`t=<ts>,v1=<hex>`) with replay protection."""
    if not secret or not sig_header or tolerance_s < 0:
        return False

    timestamp: int | None = None
    v1_signatures: list[str] = []

    for part in sig_header.split(","):
        item = part.strip()
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError:
                return False
        elif key == "v1" and value:
            v1_signatures.append(value.lower())

    if timestamp is None or not v1_signatures:
        return False

    current_ts = int(time.time()) if now is None else now
    if abs(current_ts - timestamp) > tolerance_s:
        return False

    signed_payload = f"{timestamp}.".encode() + payload
    expected = hmac.new(
        secret.encode("utf-8"),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()

    return any(hmac.compare_digest(expected, sig) for sig in v1_signatures)


class BillingStore:
    """Thread-safe store for per-org subscriptions and bounded Stripe event deduplication."""

    _MAX_PROCESSED_EVENTS = 4096

    def __init__(self, *, max_events: int = _MAX_PROCESSED_EVENTS) -> None:
        if max_events < 1:
            raise ValueError("max_events must be >= 1")
        self._lock = threading.Lock()
        self._subscriptions: dict[str, SubscriptionState] = {}
        self._processed_events: OrderedDict[str, None] = OrderedDict()
        self._max_events = max_events

    def get_subscription(self, org_id: str) -> SubscriptionState:
        with self._lock:
            existing = self._subscriptions.get(org_id)
            if existing is not None:
                return existing.model_copy()
            return SubscriptionState(org_id=org_id)

    def list_org_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._subscriptions.keys())

    def upsert_subscription(self, state: SubscriptionState) -> SubscriptionState:
        with self._lock:
            stored = state.model_copy()
            self._subscriptions[state.org_id] = stored
            return stored.model_copy()

    def get_limits(self, org_id: str) -> PlanLimits:
        sub = self.get_subscription(org_id)
        if sub.status in {"canceled", "unpaid"}:
            return PLAN_CATALOG[PlanTier.FREE]
        return PLAN_CATALOG[sub.plan]

    def find_org_by_stripe_ids(
        self,
        *,
        customer_id: str | None = None,
        subscription_id: str | None = None,
    ) -> str | None:
        with self._lock:
            for org_id, state in self._subscriptions.items():
                if subscription_id and state.stripe_subscription_id == subscription_id:
                    return org_id
                if customer_id and state.stripe_customer_id == customer_id:
                    return org_id
            return None

    def mark_event_processed(self, event_id: str) -> bool:
        """Record `event_id` if unseen. Returns True if new, False if duplicate."""
        with self._lock:
            if event_id in self._processed_events:
                return False
            self._processed_events[event_id] = None
            if len(self._processed_events) > self._max_events:
                self._processed_events.popitem(last=False)
            return True

    def is_event_processed(self, event_id: str) -> bool:
        with self._lock:
            return event_id in self._processed_events


def _parse_plan_tier(raw: Any) -> PlanTier | None:
    if not isinstance(raw, str):
        return None
    normalized = raw.strip().lower()
    try:
        return PlanTier(normalized)
    except ValueError:
        return None


def _extract_org_id(data_obj: dict[str, Any], store: BillingStore) -> str | None:
    metadata = data_obj.get("metadata")
    if isinstance(metadata, dict):
        raw_org = metadata.get("org_id")
        if isinstance(raw_org, str) and raw_org.strip():
            return raw_org.strip()

    client_ref = data_obj.get("client_reference_id")
    if isinstance(client_ref, str) and client_ref.strip():
        return client_ref.strip()

    customer_id = data_obj.get("customer") if isinstance(data_obj.get("customer"), str) else None
    sub_id = data_obj.get("subscription")
    if not isinstance(sub_id, str):
        sub_id = data_obj.get("id") if isinstance(data_obj.get("id"), str) else None

    return store.find_org_by_stripe_ids(customer_id=customer_id, subscription_id=sub_id)


def _extract_plan_from_subscription(data_obj: dict[str, Any]) -> PlanTier | None:
    metadata = data_obj.get("metadata")
    if isinstance(metadata, dict):
        plan = _parse_plan_tier(metadata.get("plan") or metadata.get("tier"))
        if plan is not None:
            return plan

    items = data_obj.get("items")
    if isinstance(items, dict):
        item_list = items.get("data")
        if isinstance(item_list, list) and item_list:
            first_item = item_list[0]
            if isinstance(first_item, dict):
                price = first_item.get("price")
                if isinstance(price, dict):
                    plan = _parse_plan_tier(price.get("lookup_key"))
                    if plan is not None:
                        return plan
                    price_meta = price.get("metadata")
                    if isinstance(price_meta, dict):
                        plan = _parse_plan_tier(price_meta.get("plan"))
                        if plan is not None:
                            return plan
    return None


def handle_stripe_webhook(
    secret: str,
    body: bytes,
    sig_header: str,
    store: BillingStore,
    *,
    now: int | None = None,
) -> tuple[int, dict[str, Any]]:
    """Verify, deduplicate, and apply Stripe billing webhook events."""
    if not verify_stripe_signature(secret, body, sig_header, now=now):
        return 401, {"error": "invalid_signature"}

    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 400, {"error": "invalid_json"}

    if not isinstance(parsed, dict):
        return 400, {"error": "invalid_payload"}

    event_id = parsed.get("id")
    event_type = parsed.get("type")
    if (
        not isinstance(event_id, str)
        or not event_id
        or not isinstance(event_type, str)
        or not event_type
    ):
        return 400, {"error": "missing_event_fields"}

    if not store.mark_event_processed(event_id):
        return 200, {"received": True, "duplicate": True, "event_id": event_id}

    data = parsed.get("data")
    data_obj: dict[str, Any] = (
        data.get("object", {}) if isinstance(data, dict) and isinstance(data.get("object"), dict)
        else {}
    )

    if event_type == "checkout.session.completed":
        org_id = _extract_org_id(data_obj, store)
        if org_id is None:
            return 400, {"error": "missing_org_id", "event_id": event_id}

        metadata = data_obj.get("metadata")
        raw_plan = metadata.get("plan") if isinstance(metadata, dict) else None
        plan = _parse_plan_tier(raw_plan) or PlanTier.PRO

        customer_id = (
            data_obj.get("customer")
            if isinstance(data_obj.get("customer"), str)
            else None
        )
        subscription_id = (
            data_obj.get("subscription")
            if isinstance(data_obj.get("subscription"), str)
            else None
        )

        updated = store.upsert_subscription(
            SubscriptionState(
                org_id=org_id,
                plan=plan,
                status="active",
                stripe_customer_id=customer_id,
                stripe_subscription_id=subscription_id,
            )
        )
        return 200, {
            "received": True,
            "event_id": event_id,
            "event_type": event_type,
            "org_id": updated.org_id,
            "plan": updated.plan.value,
            "status": updated.status,
        }

    if event_type == "customer.subscription.updated":
        org_id = _extract_org_id(data_obj, store)
        if org_id is None:
            return 400, {"error": "missing_org_id", "event_id": event_id}

        current = store.get_subscription(org_id)
        parsed_plan = _extract_plan_from_subscription(data_obj)
        new_plan = parsed_plan if parsed_plan is not None else current.plan

        raw_status = data_obj.get("status")
        new_status = raw_status if isinstance(raw_status, str) and raw_status else current.status

        customer_id = (
            data_obj.get("customer")
            if isinstance(data_obj.get("customer"), str)
            else current.stripe_customer_id
        )
        subscription_id = (
            data_obj.get("id")
            if isinstance(data_obj.get("id"), str)
            else current.stripe_subscription_id
        )

        updated = store.upsert_subscription(
            SubscriptionState(
                org_id=org_id,
                plan=new_plan,
                status=new_status,
                stripe_customer_id=customer_id,
                stripe_subscription_id=subscription_id,
            )
        )
        return 200, {
            "received": True,
            "event_id": event_id,
            "event_type": event_type,
            "org_id": updated.org_id,
            "plan": updated.plan.value,
            "status": updated.status,
        }

    if event_type == "customer.subscription.deleted":
        org_id = _extract_org_id(data_obj, store)
        if org_id is None:
            return 400, {"error": "missing_org_id", "event_id": event_id}

        current = store.get_subscription(org_id)
        customer_id = (
            data_obj.get("customer")
            if isinstance(data_obj.get("customer"), str)
            else current.stripe_customer_id
        )
        subscription_id = (
            data_obj.get("id")
            if isinstance(data_obj.get("id"), str)
            else current.stripe_subscription_id
        )

        updated = store.upsert_subscription(
            SubscriptionState(
                org_id=org_id,
                plan=PlanTier.FREE,
                status="canceled",
                stripe_customer_id=customer_id,
                stripe_subscription_id=subscription_id,
            )
        )
        return 200, {
            "received": True,
            "event_id": event_id,
            "event_type": event_type,
            "org_id": updated.org_id,
            "plan": updated.plan.value,
            "status": updated.status,
        }

    return 200, {
        "received": True,
        "ignored": True,
        "event_id": event_id,
        "event_type": event_type,
    }
