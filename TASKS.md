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
