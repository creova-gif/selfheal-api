from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from selfheal_api.ledger import DriftEventRecord, DriftLedger, OrgDriftSummary


def test_record_and_list_events_in_memory() -> None:
    ledger = DriftLedger(":memory:")
    try:
        first = ledger.record_event(
            org_id="org-alpha",
            model_name="CustomerProfile",
            fingerprint="fp-001",
            has_drift=True,
            issue_count=2,
            issue_kinds=("missing_field", "type_mismatch"),
            pr_url="https://github.com/acme/repo/pull/42",
            created_at=1000.0,
        )
        second = ledger.record_event(
            org_id="org-alpha",
            model_name="OrderItem",
            fingerprint="fp-002",
            has_drift=False,
            issue_count=0,
            created_at=2000.0,
        )

        assert first.event_id == 1
        assert second.event_id == 2
        assert first.issue_kinds == ("missing_field", "type_mismatch")
        assert second.issue_kinds == ()

        with pytest.raises(ValidationError):
            first.org_id = "mutated"

        events = ledger.list_events(org_id="org-alpha")
        assert [e.event_id for e in events] == [2, 1]
        assert events[0].has_drift is False
        assert events[1].pr_url == "https://github.com/acme/repo/pull/42"
    finally:
        ledger.close()


def test_file_backed_persistence_and_reopen(tmp_path: Path) -> None:
    db_file = tmp_path / "nested" / "drift_ledger.sqlite3"
    ledger = DriftLedger(db_file)
    recorded = ledger.record_event(
        org_id="org-persist",
        model_name="Invoice",
        fingerprint="fp-inv-1",
        has_drift=True,
        issue_count=1,
        issue_kinds=("extra_field",),
        pr_url=None,
        created_at=1700000000.0,
    )
    ledger.close()

    # Reopen from disk and verify data + WAL mode
    reopened = DriftLedger(db_file)
    try:
        events = reopened.list_events(org_id="org-persist")
        assert len(events) == 1
        assert events[0] == DriftEventRecord(
            event_id=recorded.event_id,
            org_id="org-persist",
            model_name="Invoice",
            fingerprint="fp-inv-1",
            has_drift=True,
            issue_count=1,
            issue_kinds=("extra_field",),
            pr_url=None,
            created_at=1700000000.0,
        )

        with sqlite3.connect(db_file) as conn:
            mode_row = conn.execute("PRAGMA journal_mode").fetchone()
        assert mode_row is not None
        assert str(mode_row[0]).lower() == "wal"
    finally:
        reopened.close()


def test_org_isolation_and_filters() -> None:
    ledger = DriftLedger(":memory:")
    try:
        ledger.record_event("org-a", "User", "fp-a1", True, 1, ("missing_field",), created_at=10.0)
        ledger.record_event("org-a", "Order", "fp-a2", True, 2, ("type_mismatch",), created_at=20.0)
        ledger.record_event("org-b", "User", "fp-b1", True, 3, ("enum_drift",), created_at=30.0)

        org_a_events = ledger.list_events(org_id="org-a")
        assert len(org_a_events) == 2
        assert all(e.org_id == "org-a" for e in org_a_events)

        org_b_user_events = ledger.list_events(org_id="org-b", model_name="User")
        assert len(org_b_user_events) == 1
        assert org_b_user_events[0].fingerprint == "fp-b1"

        limited = ledger.list_events(limit=2)
        assert [e.org_id for e in limited] == ["org-b", "org-a"]

        with pytest.raises(ValueError, match="limit"):
            ledger.list_events(limit=0)
    finally:
        ledger.close()


def test_org_summary_aggregation() -> None:
    ledger = DriftLedger(":memory:")
    try:
        empty_summary = ledger.get_org_summary("org-empty")
        assert empty_summary == OrgDriftSummary(
            org_id="org-empty",
            total_events=0,
            drifted_events=0,
            unique_fingerprints=0,
            prs_opened=0,
            top_models={},
        )

        ledger.record_event(
            "org-1",
            "User",
            "fp-1",
            True,
            1,
            ("missing_field",),
            pr_url="https://github.com/org/repo/pull/1",
            created_at=1.0,
        )
        ledger.record_event(
            "org-1",
            "User",
            "fp-1",
            True,
            1,
            ("missing_field",),
            pr_url=None,
            created_at=2.0,
        )
        ledger.record_event(
            "org-1",
            "User",
            "fp-2",
            False,
            0,
            (),
            pr_url="",
            created_at=3.0,
        )
        ledger.record_event(
            "org-1",
            "Payment",
            "fp-3",
            True,
            2,
            ("type_mismatch", "nullability_drift"),
            pr_url="https://github.com/org/repo/pull/2",
            created_at=4.0,
        )
        # Other org should not affect org-1 summary
        ledger.record_event(
            "org-2",
            "Payment",
            "fp-99",
            True,
            5,
            ("invalid_json",),
            pr_url="https://github.com/org/repo/pull/99",
            created_at=5.0,
        )

        summary = ledger.get_org_summary("org-1")
        assert summary == OrgDriftSummary(
            org_id="org-1",
            total_events=4,
            drifted_events=3,
            unique_fingerprints=3,
            prs_opened=2,
            top_models={"User": 3, "Payment": 1},
        )
    finally:
        ledger.close()


def test_closed_ledger_raises_runtime_error_and_sets_busy_timeout() -> None:
    ledger = DriftLedger(":memory:")
    busy_timeout_row = ledger._conn.execute("PRAGMA busy_timeout").fetchone()
    assert busy_timeout_row is not None
    assert int(busy_timeout_row[0]) == 5000

    ledger.close()

    with pytest.raises(RuntimeError, match="DriftLedger is closed"):
        ledger.record_event("org-1", "User", "fp-1", True, 1)

    with pytest.raises(RuntimeError, match="DriftLedger is closed"):
        ledger.list_events(org_id="org-1")

    with pytest.raises(RuntimeError, match="DriftLedger is closed"):
        ledger.get_org_summary("org-1")
