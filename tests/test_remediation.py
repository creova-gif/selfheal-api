from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

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
    suspended: bool = False,
    register_installation: bool = True,
    plan: PlanTier = PlanTier.FREE,
) -> tuple[
    RemediationPipeline,
    MockGitHubAppClient,
    DriftLedger,
    MockAlertNotifier,
]:
    billing = BillingStore()
    billing.upsert_subscription(SubscriptionState(org_id=org_id, plan=plan, status="active"))

    registry = InstallationRegistry()
    if register_installation:
        registry.upsert(
            InstallationRecord(
                installation_id=101,
                org_id=org_id,
                account_login=org_id,
                repositories=(repo,),
                suspended=suspended,
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


def test_remediation_pr_opened_end_to_end() -> None:
    pipeline, github, ledger, notifier = _build_pipeline()
    req = RemediationRequest(
        org_id="acme",
        repo="acme/orders",
        model_name="OrderEvent",
        model_source=ORDER_MODEL_SOURCE,
    )

    outcome = pipeline.run(
        req=req,
        expected_model=OrderEvent,
        drifted_payload={"orderId": "ord-1", "amount_cents": "4200"},
        canonical_payload={"order_id": "ord-0", "amount_cents": 100},
    )

    assert outcome.status == "pr_opened"
    assert outcome.fingerprint != ""
    assert outcome.pr_url == "https://github.com/acme/orders/pull/1"
    assert outcome.zod_adapter is not None and "z.preprocess" in outcome.zod_adapter
    assert outcome.openapi_yaml is not None and "OrderEvent:" in outcome.openapi_yaml

    assert len(github.opened_prs) == 1
    assert github.opened_prs[0]["html_url"] == outcome.pr_url
    assert github.opened_prs[0]["owner"] == "acme"
    assert github.opened_prs[0]["repo"] == "orders"
    assert github.opened_prs[0]["base"] == "main"

    summary = ledger.get_org_summary("acme")
    assert summary.prs_opened == 1
    assert summary.drifted_events == 1

    assert len(notifier.alerts) == 1
    assert notifier.alerts[0].org_id == "acme"
    assert notifier.alerts[0].model_name == "OrderEvent"
    assert notifier.alerts[0].fingerprint == outcome.fingerprint
    assert notifier.alerts[0].pr_url == outcome.pr_url


def test_remediation_no_drift() -> None:
    pipeline, github, ledger, notifier = _build_pipeline()
    req = RemediationRequest(
        org_id="acme",
        repo="acme/orders",
        model_name="OrderEvent",
        model_source=ORDER_MODEL_SOURCE,
    )

    outcome = pipeline.run(
        req=req,
        expected_model=OrderEvent,
        drifted_payload={"order_id": "ord-1", "amount_cents": 4200},
    )

    assert outcome.status == "no_drift"
    assert outcome.pr_url is None
    assert len(github.opened_prs) == 0
    assert len(notifier.alerts) == 0

    summary = ledger.get_org_summary("acme")
    assert summary.total_events == 1
    assert summary.drifted_events == 0
    assert summary.prs_opened == 0


def test_remediation_rejected_installation_missing() -> None:
    pipeline, github, _, notifier = _build_pipeline(register_installation=False)
    req = RemediationRequest(
        org_id="acme",
        repo="acme/orders",
        model_name="OrderEvent",
        model_source=ORDER_MODEL_SOURCE,
    )

    outcome = pipeline.run(
        req=req,
        expected_model=OrderEvent,
        drifted_payload={"orderId": "ord-1", "amount_cents": "4200"},
    )

    assert outcome.status == "rejected_installation"
    assert outcome.pr_url is None
    assert len(github.opened_prs) == 0
    assert len(notifier.alerts) == 0


def test_remediation_rejected_installation_suspended() -> None:
    pipeline, github, _, notifier = _build_pipeline(suspended=True)
    req = RemediationRequest(
        org_id="acme",
        repo="acme/orders",
        model_name="OrderEvent",
        model_source=ORDER_MODEL_SOURCE,
    )

    outcome = pipeline.run(
        req=req,
        expected_model=OrderEvent,
        drifted_payload={"orderId": "ord-1", "amount_cents": "4200"},
    )

    assert outcome.status == "rejected_installation"
    assert outcome.pr_url is None
    assert len(github.opened_prs) == 0
    assert len(notifier.alerts) == 0


def test_remediation_rejected_installation_unlisted_repo() -> None:
    pipeline, github, _, notifier = _build_pipeline(repo="acme/other")
    req = RemediationRequest(
        org_id="acme",
        repo="acme/orders",
        model_name="OrderEvent",
        model_source=ORDER_MODEL_SOURCE,
    )

    outcome = pipeline.run(
        req=req,
        expected_model=OrderEvent,
        drifted_payload={"orderId": "ord-1", "amount_cents": "4200"},
    )

    assert outcome.status == "rejected_installation"
    assert outcome.pr_url is None
    assert len(github.opened_prs) == 0
    assert len(notifier.alerts) == 0


def test_remediation_rejected_quota() -> None:
    pipeline, github, ledger, notifier = _build_pipeline(plan=PlanTier.FREE)
    for idx in range(3):
        ledger.record_event(
            org_id="acme",
            model_name="OrderEvent",
            fingerprint=f"fp-{idx}",
            has_drift=True,
            issue_count=1,
            issue_kinds=("type_mismatch",),
            pr_url=f"https://github.com/acme/orders/pull/{idx + 1}",
        )

    req = RemediationRequest(
        org_id="acme",
        repo="acme/orders",
        model_name="OrderEvent",
        model_source=ORDER_MODEL_SOURCE,
    )
    outcome = pipeline.run(
        req=req,
        expected_model=OrderEvent,
        drifted_payload={"orderId": "ord-1", "amount_cents": "4200"},
    )

    assert outcome.status == "rejected_quota"
    assert outcome.pr_url is None
    assert len(github.opened_prs) == 0
    assert len(notifier.alerts) == 0


def test_remediation_failed_verification() -> None:
    pipeline, github, ledger, notifier = _build_pipeline()
    bad_source = """from pydantic import BaseModel


class DifferentModel(BaseModel):
    unrelated: str
"""
    req = RemediationRequest(
        org_id="acme",
        repo="acme/orders",
        model_name="OrderEvent",
        model_source=bad_source,
    )

    outcome = pipeline.run(
        req=req,
        expected_model=OrderEvent,
        drifted_payload={"orderId": "ord-1", "amount_cents": "4200"},
    )

    assert outcome.status == "failed_verification"
    assert outcome.fingerprint != ""
    assert outcome.pr_url is None
    assert len(github.opened_prs) == 0
    assert len(notifier.alerts) == 0
    assert ledger.get_org_summary("acme").prs_opened == 0


def test_remediation_concurrent_duplicate_fingerprint_deduplicates() -> None:
    pipeline, github, ledger, notifier = _build_pipeline()
    original_open = github.open_pull_request
    barrier = threading.Barrier(2)
    first_entered = threading.Event()

    def _slow_open_pr(**kwargs: str) -> dict[str, object]:
        if not first_entered.is_set():
            first_entered.set()
            time.sleep(0.05)
        return original_open(**kwargs)

    github.open_pull_request = _slow_open_pr  # type: ignore[method-assign,assignment]

    req = RemediationRequest(
        org_id="acme",
        repo="acme/orders",
        model_name="OrderEvent",
        model_source=ORDER_MODEL_SOURCE,
    )

    def _call() -> str:
        barrier.wait(timeout=2.0)
        outcome = pipeline.run(
            req=req,
            expected_model=OrderEvent,
            drifted_payload={"orderId": "ord-1", "amount_cents": "4200"},
            canonical_payload={"order_id": "ord-0", "amount_cents": 100},
        )
        return outcome.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: _call(), range(2)))

    assert sorted(statuses) == ["deduplicated", "pr_opened"]
    assert len(github.opened_prs) == 1
    assert len(notifier.alerts) == 1
    assert ledger.get_org_summary("acme").prs_opened == 1

    # Subsequent call after completion also deduplicates and returns the opened PR URL
    third = pipeline.run(
        req=req,
        expected_model=OrderEvent,
        drifted_payload={"orderId": "ord-2", "amount_cents": "99"},
        canonical_payload={"order_id": "ord-0", "amount_cents": 100},
    )
    assert third.status == "deduplicated"
    assert third.pr_url == "https://github.com/acme/orders/pull/1"
    assert len(github.opened_prs) == 1


def test_remediation_concurrent_distinct_fingerprints_respect_quota() -> None:
    # PlanTier.FREE has max_models=3; pre-populate 2 PRs so only 1 slot remains.
    pipeline, github, ledger, _ = _build_pipeline(plan=PlanTier.FREE)
    for idx in range(2):
        ledger.record_event(
            org_id="acme",
            model_name="OrderEvent",
            fingerprint=f"existing-fp-{idx}",
            has_drift=True,
            issue_count=1,
            issue_kinds=("type_mismatch",),
            pr_url=f"https://github.com/acme/orders/pull/{idx + 1}",
        )

    original_open = github.open_pull_request

    def _slow_open_pr(**kwargs: str) -> dict[str, object]:
        time.sleep(0.05)
        return original_open(**kwargs)

    github.open_pull_request = _slow_open_pr  # type: ignore[method-assign,assignment]

    req = RemediationRequest(
        org_id="acme",
        repo="acme/orders",
        model_name="OrderEvent",
        model_source=ORDER_MODEL_SOURCE,
    )
    payloads = [
        {"orderId": "ord-1", "amount_cents": "4200"},
        {"order_id": "ord-2", "amount": "4200"},
    ]
    barrier = threading.Barrier(2)

    def _run_payload(payload: dict[str, object]) -> str:
        barrier.wait(timeout=2.0)
        return pipeline.run(
            req=req,
            expected_model=OrderEvent,
            drifted_payload=payload,
        ).status

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(_run_payload, payloads))

    assert sorted(statuses) == ["pr_opened", "rejected_quota"]
    assert len(github.opened_prs) == 1
    assert ledger.get_org_summary("acme").prs_opened == 3
