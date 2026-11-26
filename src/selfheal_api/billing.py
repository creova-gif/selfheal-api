from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import threading
import time
from enum import StrEnum
from pathlib import Path
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
    """Thread-safe SQLite-backed store for per-org subscriptions and Stripe event deduplication."""

    _MAX_PROCESSED_EVENTS = 10_000

    def __init__(
        self,
        db_path: str | Path = ":memory:",
        *,
        max_processed_events: int = _MAX_PROCESSED_EVENTS,
        max_events: int | None = None,
    ) -> None:
        resolved_max = max_events if max_events is not None else max_processed_events
        if resolved_max < 1:
            raise ValueError("max_processed_events must be >= 1")

        resolved_path = str(db_path)
        if resolved_path != ":memory:" and not resolved_path.startswith("file:"):
            Path(resolved_path).parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._closed = False
        self._max_events = resolved_max
        self._conn = sqlite3.connect(resolved_path, check_same_thread=False)
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA busy_timeout=5000;")
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS subscriptions (
                    org_id TEXT PRIMARY KEY,
                    tier TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stripe_customer_id TEXT,
                    stripe_subscription_id TEXT,
                    updated_at REAL NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS customer_org_map (
                    customer_id TEXT PRIMARY KEY,
                    org_id TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS event_watermarks (
                    scope_key TEXT PRIMARY KEY,
                    created_ts INTEGER NOT NULL
                )
                """
            )
            self._conn.commit()

    def get_subscription(self, org_id: str) -> SubscriptionState:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT org_id, tier, status, stripe_customer_id, stripe_subscription_id
                FROM subscriptions
                WHERE org_id = ?
                """,
                (org_id,),
            ).fetchone()
        if row is None:
            return SubscriptionState(org_id=org_id)
        return SubscriptionState(
            org_id=str(row[0]),
            plan=PlanTier(str(row[1])),
            status=str(row[2]),
            stripe_customer_id=str(row[3]) if row[3] is not None else None,
            stripe_subscription_id=str(row[4]) if row[4] is not None else None,
        )

    def list_org_ids(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT org_id FROM subscriptions ORDER BY org_id ASC"
            ).fetchall()
        return [str(row[0]) for row in rows]

    def upsert_subscription(self, state: SubscriptionState) -> SubscriptionState:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO subscriptions (
                    org_id,
                    tier,
                    status,
                    stripe_customer_id,
                    stripe_subscription_id,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(org_id) DO UPDATE SET
                    tier = excluded.tier,
                    status = excluded.status,
                    stripe_customer_id = excluded.stripe_customer_id,
                    stripe_subscription_id = excluded.stripe_subscription_id,
                    updated_at = excluded.updated_at
                """,
                (
                    state.org_id,
                    state.plan.value,
                    state.status,
                    state.stripe_customer_id,
                    state.stripe_subscription_id,
                    now,
                ),
            )
            if state.stripe_customer_id:
                self._conn.execute(
                    """
                    INSERT INTO customer_org_map (customer_id, org_id)
                    VALUES (?, ?)
                    ON CONFLICT(customer_id) DO UPDATE SET
                        org_id = excluded.org_id
                    """,
                    (state.stripe_customer_id, state.org_id),
                )
            self._conn.commit()
        return state.model_copy()

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
            if subscription_id:
                row = self._conn.execute(
                    """
                    SELECT org_id FROM subscriptions
                    WHERE stripe_subscription_id = ?
                    LIMIT 1
                    """,
                    (subscription_id,),
                ).fetchone()
                if row is not None:
                    return str(row[0])
            if customer_id:
                row = self._conn.execute(
                    """
                    SELECT org_id FROM subscriptions
                    WHERE stripe_customer_id = ?
                    LIMIT 1
                    """,
                    (customer_id,),
                ).fetchone()
                if row is not None:
                    return str(row[0])
                map_row = self._conn.execute(
                    """
                    SELECT org_id FROM customer_org_map
                    WHERE customer_id = ?
                    LIMIT 1
                    """,
                    (customer_id,),
                ).fetchone()
                if map_row is not None:
                    return str(map_row[0])
            return None

    def mark_event_processed(self, event_id: str) -> bool:
        """Record `event_id` if unseen. Returns True if new, False if duplicate."""
        with self._lock:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO processed_events (event_id) VALUES (?)",
                (event_id,),
            )
            if cursor.rowcount == 0:
                return False
            self._conn.execute(
                """
                DELETE FROM processed_events
                WHERE seq NOT IN (
                    SELECT seq FROM processed_events
                    ORDER BY seq DESC
                    LIMIT ?
                )
                """,
                (self._max_events,),
            )
            self._conn.commit()
            return True

    def is_event_processed(self, event_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM processed_events WHERE event_id = ? LIMIT 1",
                (event_id,),
            ).fetchone()
            return row is not None

    def is_stale_event(
        self,
        *,
        org_id: str,
        subscription_id: str | None = None,
        created: int | None = None,
    ) -> bool:
        """Return True if a newer Stripe event timestamp has already been applied."""
        if created is None:
            return False
        with self._lock:
            org_row = self._conn.execute(
                "SELECT created_ts FROM event_watermarks WHERE scope_key = ?",
                (f"org:{org_id}",),
            ).fetchone()
            if org_row is not None and created < int(org_row[0]):
                return True
            if subscription_id:
                sub_row = self._conn.execute(
                    "SELECT created_ts FROM event_watermarks WHERE scope_key = ?",
                    (f"sub:{subscription_id}",),
                ).fetchone()
                if sub_row is not None and created < int(sub_row[0]):
                    return True
            return False

    def record_event_created(
        self,
        *,
        org_id: str,
        subscription_id: str | None = None,
        created: int | None = None,
    ) -> None:
        """Record the latest processed Stripe event `created` timestamp."""
        if created is None:
            return
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO event_watermarks (scope_key, created_ts)
                VALUES (?, ?)
                ON CONFLICT(scope_key) DO UPDATE SET
                    created_ts = MAX(event_watermarks.created_ts, excluded.created_ts)
                """,
                (f"org:{org_id}", created),
            )
            if subscription_id:
                self._conn.execute(
                    """
                    INSERT INTO event_watermarks (scope_key, created_ts)
                    VALUES (?, ?)
                    ON CONFLICT(scope_key) DO UPDATE SET
                        created_ts = MAX(event_watermarks.created_ts, excluded.created_ts)
                    """,
                    (f"sub:{subscription_id}", created),
                )
            self._conn.commit()

    def ping(self) -> bool:
        """Check database connectivity, returning False if closed or on SQLite errors."""
        with self._lock:
            if self._closed:
                return False
            try:
                self._conn.execute("SELECT 1").fetchone()
            except sqlite3.Error:
                return False
        return True

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True


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

    if store.is_event_processed(event_id):
        return 200, {"received": True, "duplicate": True, "event_id": event_id}

    raw_created = parsed.get("created")
    event_created = (
        raw_created
        if isinstance(raw_created, int) and not isinstance(raw_created, bool)
        else None
    )

    data = parsed.get("data")
    data_obj: dict[str, Any] = (
        data.get("object", {}) if isinstance(data, dict) and isinstance(data.get("object"), dict)
        else {}
    )

    if event_type == "checkout.session.completed":
        org_id = _extract_org_id(data_obj, store)
        if org_id is None:
            return 400, {"error": "missing_org_id", "event_id": event_id, "retryable": True}

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

        if not store.mark_event_processed(event_id):
            return 200, {"received": True, "duplicate": True, "event_id": event_id}
        if store.is_stale_event(
            org_id=org_id, subscription_id=subscription_id, created=event_created
        ):
            return 200, {
                "received": True,
                "ignored": True,
                "stale": True,
                "event_id": event_id,
                "event_type": event_type,
                "org_id": org_id,
            }

        updated = store.upsert_subscription(
            SubscriptionState(
                org_id=org_id,
                plan=plan,
                status="active",
                stripe_customer_id=customer_id,
                stripe_subscription_id=subscription_id,
            )
        )
        store.record_event_created(
            org_id=org_id, subscription_id=subscription_id, created=event_created
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
            return 400, {"error": "missing_org_id", "event_id": event_id, "retryable": True}

        current = store.get_subscription(org_id)
        subscription_id = (
            data_obj.get("id")
            if isinstance(data_obj.get("id"), str)
            else current.stripe_subscription_id
        )

        if not store.mark_event_processed(event_id):
            return 200, {"received": True, "duplicate": True, "event_id": event_id}
        if store.is_stale_event(
            org_id=org_id, subscription_id=subscription_id, created=event_created
        ):
            return 200, {
                "received": True,
                "ignored": True,
                "stale": True,
                "event_id": event_id,
                "event_type": event_type,
                "org_id": org_id,
            }

        parsed_plan = _extract_plan_from_subscription(data_obj)
        new_plan = parsed_plan if parsed_plan is not None else current.plan

        raw_status = data_obj.get("status")
        new_status = raw_status if isinstance(raw_status, str) and raw_status else current.status

        customer_id = (
            data_obj.get("customer")
            if isinstance(data_obj.get("customer"), str)
            else current.stripe_customer_id
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
        store.record_event_created(
            org_id=org_id, subscription_id=subscription_id, created=event_created
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
            return 400, {"error": "missing_org_id", "event_id": event_id, "retryable": True}

        current = store.get_subscription(org_id)
        subscription_id = (
            data_obj.get("id")
            if isinstance(data_obj.get("id"), str)
            else current.stripe_subscription_id
        )

        if not store.mark_event_processed(event_id):
            return 200, {"received": True, "duplicate": True, "event_id": event_id}
        if store.is_stale_event(
            org_id=org_id, subscription_id=subscription_id, created=event_created
        ):
            return 200, {
                "received": True,
                "ignored": True,
                "stale": True,
                "event_id": event_id,
                "event_type": event_type,
                "org_id": org_id,
            }

        customer_id = (
            data_obj.get("customer")
            if isinstance(data_obj.get("customer"), str)
            else current.stripe_customer_id
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
        store.record_event_created(
            org_id=org_id, subscription_id=subscription_id, created=event_created
        )
        return 200, {
            "received": True,
            "event_id": event_id,
            "event_type": event_type,
            "org_id": updated.org_id,
            "plan": updated.plan.value,
            "status": updated.status,
        }

    if not store.mark_event_processed(event_id):
        return 200, {"received": True, "duplicate": True, "event_id": event_id}
    return 200, {
        "received": True,
        "ignored": True,
        "event_id": event_id,
        "event_type": event_type,
    }
