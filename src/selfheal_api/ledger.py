from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class DriftEventRecord(BaseModel):
    """Immutable structural record of a single drift evaluation (zero PII)."""

    model_config = ConfigDict(frozen=True)

    event_id: int | None = None
    org_id: str
    model_name: str
    fingerprint: str
    has_drift: bool
    issue_count: int
    issue_kinds: tuple[str, ...] = ()
    pr_url: str | None = None
    created_at: float


class OrgDriftSummary(BaseModel):
    """Aggregated drift metrics for a single organization."""

    model_config = ConfigDict(frozen=True)

    org_id: str
    total_events: int
    drifted_events: int
    unique_fingerprints: int
    prs_opened: int
    top_models: dict[str, int]


class DriftLedger:
    """Thread-safe SQLite-backed ledger for zero-PII schema drift events."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        resolved_path = str(db_path)
        if resolved_path != ":memory:" and not resolved_path.startswith("file:"):
            Path(resolved_path).parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._closed = False
        self._conn = sqlite3.connect(resolved_path, check_same_thread=False)
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA busy_timeout=5000;")
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS drift_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    org_id TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    has_drift INTEGER NOT NULL,
                    issue_count INTEGER NOT NULL,
                    issue_kinds_json TEXT NOT NULL,
                    pr_url TEXT,
                    created_at REAL NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_drift_events_org_created
                ON drift_events (org_id, created_at DESC)
                """
            )
            self._conn.commit()

    def record_event(
        self,
        org_id: str,
        model_name: str,
        fingerprint: str,
        has_drift: bool,
        issue_count: int,
        issue_kinds: tuple[str, ...] | list[str] = (),
        pr_url: str | None = None,
        created_at: float | None = None,
    ) -> DriftEventRecord:
        """Persist a structural drift event and return the stored record."""
        created = time.time() if created_at is None else created_at
        kinds_tuple = tuple(issue_kinds)
        kinds_json = json.dumps(list(kinds_tuple))

        with self._lock:
            if self._closed:
                raise RuntimeError("DriftLedger is closed")
            cursor = self._conn.execute(
                """
                INSERT INTO drift_events (
                    org_id,
                    model_name,
                    fingerprint,
                    has_drift,
                    issue_count,
                    issue_kinds_json,
                    pr_url,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    org_id,
                    model_name,
                    fingerprint,
                    1 if has_drift else 0,
                    issue_count,
                    kinds_json,
                    pr_url,
                    created,
                ),
            )
            self._conn.commit()
            event_id = cursor.lastrowid

        return DriftEventRecord(
            event_id=event_id,
            org_id=org_id,
            model_name=model_name,
            fingerprint=fingerprint,
            has_drift=has_drift,
            issue_count=issue_count,
            issue_kinds=kinds_tuple,
            pr_url=pr_url,
            created_at=created,
        )

    def list_events(
        self,
        org_id: str | None = None,
        model_name: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[DriftEventRecord]:
        """List recent drift events, optionally filtered by org_id and model_name."""
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if offset < 0:
            raise ValueError("offset must be >= 0")

        clauses: list[str] = []
        params: list[object] = []

        if org_id is not None:
            clauses.append("org_id = ?")
            params.append(org_id)
        if model_name is not None:
            clauses.append("model_name = ?")
            params.append(model_name)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        params.append(offset)

        query = f"""
            SELECT
                event_id,
                org_id,
                model_name,
                fingerprint,
                has_drift,
                issue_count,
                issue_kinds_json,
                pr_url,
                created_at
            FROM drift_events
            {where_sql}
            ORDER BY created_at DESC, event_id DESC
            LIMIT ? OFFSET ?
        """

        with self._lock:
            if self._closed:
                raise RuntimeError("DriftLedger is closed")
            rows = self._conn.execute(query, params).fetchall()

        return [
            DriftEventRecord(
                event_id=int(row[0]),
                org_id=str(row[1]),
                model_name=str(row[2]),
                fingerprint=str(row[3]),
                has_drift=bool(row[4]),
                issue_count=int(row[5]),
                issue_kinds=tuple(str(item) for item in json.loads(str(row[6]))),
                pr_url=str(row[7]) if row[7] is not None else None,
                created_at=float(row[8]),
            )
            for row in rows
        ]

    def get_org_summary(self, org_id: str) -> OrgDriftSummary:
        """Compute aggregated drift statistics for a single organization."""
        with self._lock:
            if self._closed:
                raise RuntimeError("DriftLedger is closed")
            agg_row = self._conn.execute(
                """
                SELECT
                    COUNT(*),
                    COALESCE(SUM(CASE WHEN has_drift = 1 THEN 1 ELSE 0 END), 0),
                    COUNT(DISTINCT fingerprint),
                    COUNT(CASE WHEN pr_url IS NOT NULL AND pr_url != '' THEN 1 END)
                FROM drift_events
                WHERE org_id = ?
                """,
                (org_id,),
            ).fetchone()

            model_rows = self._conn.execute(
                """
                SELECT model_name, COUNT(*) AS event_count
                FROM drift_events
                WHERE org_id = ?
                GROUP BY model_name
                ORDER BY event_count DESC, model_name ASC
                LIMIT 10
                """,
                (org_id,),
            ).fetchall()

        assert agg_row is not None
        top_models = {str(row[0]): int(row[1]) for row in model_rows}

        return OrgDriftSummary(
            org_id=org_id,
            total_events=int(agg_row[0]),
            drifted_events=int(agg_row[1]),
            unique_fingerprints=int(agg_row[2]),
            prs_opened=int(agg_row[3]),
            top_models=top_models,
        )

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

    def global_totals(self) -> dict[str, int]:
        """Return global event counts across all organizations."""
        with self._lock:
            if self._closed:
                raise RuntimeError("DriftLedger is closed")
            row = self._conn.execute(
                """
                SELECT
                    COUNT(*),
                    COALESCE(SUM(CASE WHEN has_drift = 1 THEN 1 ELSE 0 END), 0),
                    COUNT(CASE WHEN pr_url IS NOT NULL AND pr_url != '' THEN 1 END)
                FROM drift_events
                """
            ).fetchone()
        assert row is not None
        return {
            "total_events": int(row[0]),
            "drifted_events": int(row[1]),
            "prs_opened": int(row[2]),
        }

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True
