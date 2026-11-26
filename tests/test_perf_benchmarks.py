from __future__ import annotations

import time
from pathlib import Path

from pydantic import BaseModel

from selfheal_api.ledger import DriftLedger
from sentinel.detector import DriftKind, detect_drift
from sentinel.guard import DriftDeduplicator
from synthesizer.patcher import (
    PatchAction,
    PatchPlan,
    apply_patch_plan_in_memory,
    build_patch_plan,
)


class _BillingAddress(BaseModel):
    postal_code: str
    city: str


class _CustomerOrder(BaseModel):
    order_id: int
    address: _BillingAddress


def test_perf_detect_plan_and_in_memory_heal_throughput() -> None:
    """200 iterations of detect -> plan -> in-memory heal complete under 3.0s."""
    drifted_payload: dict[str, object] = {
        "order_id": 42,
        "address": {
            "zip_code": "10001",
            "city": "New York",
        },
        "legacy_flag": True,
    }

    start = time.perf_counter()
    for _ in range(200):
        report = detect_drift(_CustomerOrder, drifted_payload)
        assert report.has_drift is True

        plan = build_patch_plan(report, root_model=_CustomerOrder)
        assert len(plan.actions) >= 1

        healed = apply_patch_plan_in_memory(
            drifted_payload,
            plan,
            root_model=_CustomerOrder,
        )
        assert healed["order_id"] == 42
        healed_address = healed["address"]
        assert isinstance(healed_address, dict)
        assert healed_address["postal_code"] == "10001"
        assert healed_address["city"] == "New York"
        validated = _CustomerOrder.model_validate(healed)
        assert validated.address.postal_code == "10001"
    elapsed = time.perf_counter() - start

    assert elapsed < 3.0, f"200 detect/plan/heal iterations took {elapsed:.3f}s (>= 3.0s)"


def test_perf_drift_ledger_write_and_query_throughput(tmp_path: Path) -> None:
    """500 ledger writes + org summary, event list, and global totals complete under 3.0s."""
    db_path = tmp_path / "perf.db"
    ledger = DriftLedger(db_path)
    try:
        start = time.perf_counter()
        for idx in range(500):
            has_drift = idx % 5 != 0
            ledger.record_event(
                org_id="acme",
                model_name="CustomerOrder",
                fingerprint=f"fp-{idx % 50:02d}",
                has_drift=has_drift,
                issue_count=1 if has_drift else 0,
                issue_kinds=(DriftKind.MISSING_FIELD.value,) if has_drift else (),
                pr_url=f"https://github.com/acme/repo/pull/{idx}" if idx % 10 == 0 else None,
                created_at=1_700_000_000.0 + float(idx),
            )

        summary = ledger.get_org_summary("acme")
        events = ledger.list_events("acme", limit=50)
        totals = ledger.global_totals()
        elapsed = time.perf_counter() - start
    finally:
        ledger.close()

    assert summary.org_id == "acme"
    assert summary.total_events == 500
    assert summary.drifted_events == 400
    assert summary.unique_fingerprints == 50
    assert summary.prs_opened == 50
    assert len(events) == 50
    assert totals == {
        "total_events": 500,
        "drifted_events": 400,
        "prs_opened": 50,
    }
    assert elapsed < 3.0, f"500 ledger writes + queries took {elapsed:.3f}s (>= 3.0s)"


def test_perf_deduplicator_fast_path_throughput() -> None:
    """1,000 cached DriftDeduplicator.reserve() lookups complete under 1.0s."""
    dedup = DriftDeduplicator()
    fingerprint = "a" * 64
    plan = PatchPlan(
        model_name="CustomerOrder",
        actions=(
            PatchAction(
                target_model="CustomerOrder",
                target_field="postal_code",
                drift_kind=DriftKind.MISSING_FIELD,
                expected_type="str",
                observed_kind="missing",
                source_path=("zip_code",),
            ),
        ),
    )

    is_owner, initial = dedup.reserve(fingerprint)
    assert is_owner is True
    assert initial is None
    dedup.complete(fingerprint, plan)

    start = time.perf_counter()
    for _ in range(1000):
        owner, cached = dedup.reserve(fingerprint)
        assert owner is False
        assert cached == plan
    elapsed = time.perf_counter() - start

    assert elapsed < 1.0, f"1,000 deduplicator fast-path lookups took {elapsed:.3f}s (>= 1.0s)"
