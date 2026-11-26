# selfheal-api — Execution Checklist

## Wave 1: Day 1–5 Setup
- [x] Set up sentinel/, synthesizer/, and tests/ directories
- [x] Implement detect_drift(expected_model, raw_json) with redacted payload snapshots
- [x] Build 10-mutation test suite simulating Stripe/HubSpot/Weather API drift
- [x] Verify libcst/Claude patch passes pytest and mypy cleanly

## Wave 2: MVP & Eval Gate
- [x] Write ADR-001 in `docs/adr/`
- [x] Implement core pipeline + deterministic unit/eval tests
- [x] Verify CI workflow passes (`ruff`/`mypy` or `cargo clippy`/`test` or `go test -race`)

## Wave 3: Launch & Design Partner Validation
- [x] Publish clean README with reproducible benchmark numbers
- [x] Implement `EXP-1`–`EXP-5` (`selfheal` CLI, zero-PII `AnthropicSchemaMappingProvider`, `@selfheal_guard` + `DriftDeduplicator`, nested/list synthesis, TypeScript/Zod emitter)
- [x] Implement Hosted Multi-Tenant Webhook Server (`python -m selfheal_api.server`, `/webhooks/drift`, `/healthz`) and Cloud Run `Dockerfile`
- [ ] Run first external design-partner or open-source release

## Wave 4: Milestone 4 — Full Product Engineering & Scope Expansion
- [x] Persistent SQLite zero-PII Drift Event Ledger (`src/selfheal_api/ledger.py`)
- [x] Zero-PII Slack / Webhook Alert Notifier (`src/selfheal_api/notifier.py`)
- [x] OpenAPI 3.1 Schema Diff & `x-selfheal-aliases` Emitter (`src/synthesizer/openapi_emitter.py`)
- [x] Self-contained XSS-safe Drift Console UI (`src/selfheal_api/dashboard.py`, `GET /dashboard`) & Ledger API (`GET /api/v1/events`, `GET /api/v1/summary`)

## Wave 5: Milestone 5 — Middleware, Auto-Remediation & OpenAPI CLI
- [x] Zero-dependency ASGI/WSGI schema-drift middleware (`SelfHealASGIMiddleware`, `SelfHealWSGIMiddleware`)
- [x] Fail-closed `RemediationPipeline` orchestrating detection, CST synthesis, verification gates, and rollback
- [x] `--openapi` CLI flag on `selfheal heal` emitting OpenAPI 3.1.0 `components.schemas` JSON with `x-selfheal-aliases` and rollback cleanup

## Wave 6: Milestone 6 — Background Worker Queue, Replay Harness & SDK Exports
- [x] Bounded non-blocking `RemediationQueue` background worker with fingerprint deduplication and graceful drain
- [x] Zero-PII `run_replay_corpus` deterministic regression harness for CI evaluation
- [x] Public `selfheal_api` SDK `__all__` exports and `__version__ = "0.2.0"`
- [x] Document ADR-006 (`docs/adr/006-background-remediation-worker-and-replay-harness.md`), `TASKS.md`, and `CHANGELOG.md`

## Wave 7: Milestone 7 — Enterprise Hardening, Zero-Exec Runtime & Observability
- [x] Pure-Python `apply_patch_plan_in_memory` refactor removing runtime `exec()`
- [x] `RemediationPipeline` concurrency lock for safe serialized remediation & rollback
- [x] Enterprise observability: `/readyz` readiness probe, `X-Request-Id` correlation, and Prometheus metrics
- [x] `selfheal doctor` CLI subcommand (`--json` and human-readable checklist, zero-secret `ANTHROPIC_API_KEY` check)
- [x] Document ADR-007 (`docs/adr/007-runtime-exec-removal-and-enterprise-observability.md`) and update `TASKS.md`

## Wave 8: Milestone 8 — Durable Control-Plane Persistence, Hardening & CI Matrix
- [x] SQLite WAL persistence for `BillingStore` and `InstallationRegistry` sharing `SELFHEAL_DB_PATH`
- [x] Graceful `SIGTERM` shutdown handling and container `HEALTHCHECK`
- [x] HTTP security response headers on server endpoints
- [x] Deterministic performance benchmark suite for hot-path drift detection and normalization
- [x] Python 3.12 / 3.13 CI matrix with `selfheal doctor --json` gate in `.github/workflows/ci.yml`
- [x] Document ADR-008 (`docs/adr/008-durable-control-plane-persistence.md`), `TASKS.md`, and `CHANGELOG.md`
