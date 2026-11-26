from __future__ import annotations

import threading

from pydantic import BaseModel

from selfheal_api.billing import BillingStore, PlanTier, SubscriptionState
from selfheal_api.github_app import (
    InstallationRecord,
    InstallationRegistry,
    MockGitHubAppClient,
)
from selfheal_api.ledger import DriftLedger
from selfheal_api.notifier import MockAlertNotifier
from selfheal_api.remediation import RemediationPipeline, RemediationRequest
from selfheal_api.worker import QueuedRemediationJob, RemediationQueue


class OrderEvent(BaseModel):
    order_id: str
    amount_cents: int


ORDER_MODEL_SOURCE = """from pydantic import BaseModel


class OrderEvent(BaseModel):
    order_id: str
    amount_cents: int
"""


def _build_pipeline(
    *,
    org_id: str = "acme",
    repo: str = "acme/orders",
    extra_orgs: tuple[tuple[str, str], ...] = (),
) -> tuple[
    RemediationPipeline,
    MockGitHubAppClient,
    DriftLedger,
    MockAlertNotifier,
]:
    billing = BillingStore()
    billing.upsert_subscription(
        SubscriptionState(org_id=org_id, plan=PlanTier.PRO, status="active")
    )

    registry = InstallationRegistry()
    registry.upsert(
        InstallationRecord(
            installation_id=101,
            org_id=org_id,
            account_login=org_id,
            repositories=(repo,),
            suspended=False,
        )
    )
    for idx, (extra_org, extra_repo) in enumerate(extra_orgs, start=102):
        billing.upsert_subscription(
            SubscriptionState(org_id=extra_org, plan=PlanTier.PRO, status="active")
        )
        registry.upsert(
            InstallationRecord(
                installation_id=idx,
                org_id=extra_org,
                account_login=extra_org,
                repositories=(extra_repo,),
                suspended=False,
            )
        )

    github = MockGitHubAppClient()
    ledger = DriftLedger(":memory:")
    notifier = MockAlertNotifier()
    pipeline = RemediationPipeline(
        billing_store=billing,
        installation_registry=registry,
        github_client=github,
        drift_ledger=ledger,
        notifier=notifier,
    )
    return pipeline, github, ledger, notifier


def _make_job(
    *,
    org_id: str = "acme",
    repo: str = "acme/orders",
    fingerprint: str = "fp-1",
    drifted_payload: dict[str, object] | None = None,
) -> QueuedRemediationJob:
    return QueuedRemediationJob(
        request=RemediationRequest(
            org_id=org_id,
            repo=repo,
            model_name="OrderEvent",
            model_source=ORDER_MODEL_SOURCE,
        ),
        expected_model=OrderEvent,
        drifted_payload=drifted_payload or {"orderId": "ord-1", "amount_cents": "4200"},
        canonical_payload={"order_id": "ord-0", "amount_cents": 100},
        fingerprint=fingerprint,
    )


def test_worker_async_pr_opened_and_wait_until_drained() -> None:
    pipeline, github, ledger, notifier = _build_pipeline()
    worker = RemediationQueue(pipeline)
    worker.start()
    try:
        accepted = worker.enqueue(_make_job(fingerprint="fp-async-1"))
        assert accepted is True
        assert worker.wait_until_drained(timeout=5.0) is True

        stats = worker.stats()
        assert stats.enqueued == 1
        assert stats.processed == 1
        assert stats.succeeded == 1
        assert stats.failed == 0
        assert stats.dropped_full == 0
        assert stats.deduplicated == 0

        assert len(github.opened_prs) == 1
        assert github.opened_prs[0]["html_url"] == "https://github.com/acme/orders/pull/1"
        assert ledger.get_org_summary("acme").prs_opened == 1
        assert len(notifier.alerts) == 1
    finally:
        worker.stop(timeout=5.0)


def test_worker_deduplicates_org_and_fingerprint() -> None:
    pipeline, github, _, _ = _build_pipeline(extra_orgs=(("globex", "globex/orders"),))
    worker = RemediationQueue(pipeline, max_seen_fingerprints=2)
    worker.start()
    try:
        assert worker.enqueue(_make_job(org_id="acme", repo="acme/orders", fingerprint="fp-dup"))
        # Same org + same fingerprint -> deduplicated
        assert (
            worker.enqueue(_make_job(org_id="acme", repo="acme/orders", fingerprint="fp-dup"))
            is False
        )
        # Different org + same fingerprint -> NOT deduplicated
        assert (
            worker.enqueue(_make_job(org_id="globex", repo="globex/orders", fingerprint="fp-dup"))
            is True
        )
        # Push a third unique key to evict ("acme", "fp-dup") from LRU cache (size=2)
        assert (
            worker.enqueue(_make_job(org_id="acme", repo="acme/orders", fingerprint="fp-new"))
            is True
        )
        # ("acme", "fp-dup") was evicted, so it can be enqueued again
        assert (
            worker.enqueue(_make_job(org_id="acme", repo="acme/orders", fingerprint="fp-dup"))
            is True
        )

        assert worker.wait_until_drained(timeout=5.0) is True
        stats = worker.stats()
        assert stats.enqueued == 4
        assert stats.deduplicated == 1
        assert stats.processed == 4
        assert stats.succeeded == 4
        assert len(github.opened_prs) == 4
    finally:
        worker.stop(timeout=5.0)


def test_worker_backpressure_drops_when_full() -> None:
    pipeline, _, _, _ = _build_pipeline()
    worker = RemediationQueue(pipeline, max_queue_size=2)

    # Enqueue before starting worker so the bounded queue fills deterministically.
    assert worker.enqueue(_make_job(fingerprint="fp-1")) is True
    assert worker.enqueue(_make_job(fingerprint="fp-2")) is True
    assert worker.enqueue(_make_job(fingerprint="fp-3")) is False

    stats_before = worker.stats()
    assert stats_before.enqueued == 2
    assert stats_before.dropped_full == 1

    worker.start()
    try:
        assert worker.wait_until_drained(timeout=5.0) is True
        # Dropped fingerprint was not recorded as seen, so it can be enqueued after draining.
        assert worker.enqueue(_make_job(fingerprint="fp-3")) is True
        assert worker.wait_until_drained(timeout=5.0) is True

        stats_after = worker.stats()
        assert stats_after.enqueued == 3
        assert stats_after.processed == 3
        assert stats_after.succeeded == 3
        assert stats_after.dropped_full == 1
    finally:
        worker.stop(timeout=5.0)


def test_worker_clean_stop_and_idempotency() -> None:
    pipeline, _, _, _ = _build_pipeline()
    worker = RemediationQueue(pipeline)

    worker.start()
    worker.start()  # Idempotent start
    thread = worker._thread
    assert thread is not None
    assert thread.is_alive()
    assert thread.daemon is True

    worker.stop(timeout=5.0)
    assert not thread.is_alive()
    assert worker._thread is None

    # Idempotent stop
    worker.stop(timeout=5.0)


def test_worker_wait_until_drained_timeout_and_failure_counting() -> None:
    pipeline, _, _, _ = _build_pipeline()
    gate = threading.Event()
    original_run = pipeline.run

    def slow_or_failing_run(
        req: RemediationRequest,
        expected_model: type[BaseModel],
        drifted_payload: dict[str, object],
        canonical_payload: dict[str, object] | None = None,
    ) -> object:
        gate.wait(timeout=2.0)
        if req.org_id == "boom":
            raise RuntimeError("simulated failure")
        return original_run(req, expected_model, drifted_payload, canonical_payload)

    pipeline.run = slow_or_failing_run  # type: ignore[method-assign,assignment]

    worker = RemediationQueue(pipeline)
    worker.start()
    try:
        assert worker.enqueue(_make_job(org_id="boom", fingerprint="fp-fail")) is True
        assert worker.wait_until_drained(timeout=0.05) is False

        gate.set()
        assert worker.wait_until_drained(timeout=5.0) is True
        stats = worker.stats()
        assert stats.processed == 1
        assert stats.failed == 1
        assert stats.succeeded == 0
    finally:
        gate.set()
        worker.stop(timeout=5.0)
