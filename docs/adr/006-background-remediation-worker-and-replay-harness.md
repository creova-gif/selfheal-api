# ADR-006: Bounded Background Remediation Worker and Zero-PII Replay Harness
- **Status:** Accepted
- **Owner:** Justin Mafie

## Context
Following Milestone 5's `SelfHealASGIMiddleware`, `SelfHealWSGIMiddleware`, and fail-closed `RemediationPipeline`, two operational gaps remained before production deployment:
1. **Non-Blocking Remediation Execution:** Running `RemediationPipeline` (LibCST synthesis, `verify_patch`, `mypy --strict --shadow-file`, and `pytest`) takes hundreds of milliseconds to seconds and must never block HTTP request handlers or `@selfheal_guard` call sites. At the same time, an unbounded background queue risks memory exhaustion under sustained schema-drift storms.
2. **Deterministic Regression Verification:** Teams need a repeatable way to benchmark and regression-test `detect_drift`, `synthesize_patch`, and `verify_patch` across synthetic or redacted drift corpora without storing or replaying raw PII/PHI payloads.

## Decision
1. **Bounded Non-Blocking `RemediationQueue` Worker:**
   - Implement a thread-safe, bounded background worker (`RemediationQueue`) backed by a fixed-capacity queue (`max_queue_size`) and fingerprint deduplication (`compute_drift_fingerprint`).
   - Enqueue operations (`enqueue` / `submit`) are non-blocking: when the queue is full or a drift fingerprint is already pending/completed, new submissions are dropped or coalesced immediately with explicit status telemetry rather than blocking callers.
   - Worker execution delegates to `RemediationPipeline` with fail-closed semantics and clean shutdown (`stop` / `drain` with bounded timeout).
2. **Zero-PII `run_replay_corpus` Regression Harness:**
   - Implement a deterministic replay harness (`run_replay_corpus`) that evaluates a corpus of redacted/synthetic drift fixtures against expected `DriftReport` and `VerificationResult` outcomes.
   - Enforce zero-PII invariants on corpus fixtures (no raw secrets, emails, tokens, or unredacted customer scalars) and report structured pass/fail metrics (recall, verification pass rate, and per-case diagnostics) suitable for CI gating.
3. **Public SDK Surface (`selfheal_api.__init__`):**
   - Expose `__version__ = "0.2.0"` and a curated `__all__` surface re-exporting core detection, synthesis, OpenAPI 3.1 emission, and ASGI/WSGI middleware primitives from `selfheal_api`.

## Alternatives Considered
- **Unbounded `ThreadPoolExecutor` / `asyncio.create_task` Fire-and-Forget:** Rejected because bursty drift traffic would spawn unbounded concurrent `mypy`/`pytest` subprocesses, exhausting CPU and memory.
- **External Broker Requirement (Celery / Redis) for Core SDK:** Rejected to preserve `selfheal-api`'s zero-infrastructure core; teams can still forward `DriftReport` events to external queues via webhook/notifier hooks.
- **Raw Production Payload Snapshots in Regression Corpus:** Rejected because storing unredacted HTTP bodies violates `AGENTS.md` zero-PII/PHI security standards.

## Consequences & Failure Modes
- **Backpressure Drops:** When `RemediationQueue` reaches `max_queue_size`, excess drift events are dropped at enqueue time; callers must monitor drop counters and rely on `DriftDeduplicator` / ledger alerts for visibility.
- **Process-Local State:** The in-process `RemediationQueue` does not persist pending jobs across container restarts; durable tracking relies on the SQLite Drift Event Ledger (`src/selfheal_api/ledger.py`).
- **Corpus Drift:** `run_replay_corpus` only guards against scenarios represented in the corpus; new production drift patterns should be distilled into redacted synthetic fixtures and added to the corpus.
