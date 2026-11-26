# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Milestone 9 (CLI Scaffolding & Schema Export)**:
  - Added `selfheal init` (`--dir`, `--force`) to scaffold `selfheal.config.json` and `.github/workflows/selfheal.yml` without clobbering existing files unless `--force` is passed.
  - Added `selfheal schema` (`--model`, `--format jsonschema|openapi|zod`, `--out`) to export Pydantic model schemas to stdout or an atomic output file.
- **Milestone 8 (Durable Control Plane, Hardening & CI Matrix)**:
  - Added SQLite WAL persistence for `BillingStore` and `InstallationRegistry` sharing `SELFHEAL_DB_PATH` for durable control-plane state across restarts.
  - Added graceful `SIGTERM` shutdown handling, container `HEALTHCHECK`, and HTTP security response headers.
  - Added deterministic performance benchmark gates in the test suite.
  - Expanded `.github/workflows/ci.yml` to a Python 3.12 / 3.13 matrix (`fail-fast: false`) running `ruff check src tests`, `mypy --strict src tests`, `pytest -v`, and `selfheal doctor --json`.
  - Documented `docs/adr/008-durable-control-plane-persistence.md`.

## [0.2.0] - 2026-11-26

### Added
- **EXP-1 (`selfheal` CLI)**: Added `selfheal check` and `selfheal heal` subcommands (`--diff`, `--write`, `--zod`, `--open-pr`, `--branch`, `--canonical`, `--llm-fallback`, `--pytest-args`) with atomic file writes and 3-stage verification (`verify_patch`, `mypy --strict --shadow-file`, `pytest`).
- **EXP-2 (`AnthropicSchemaMappingProvider`)**: Added zero-PII LLM fallback mapping provider sending only redacted `PrimitiveKind` schemas and field names, with thread-safe `max_calls` rate budgeting and `DeterministicMappingProvider` for offline testing.
- **EXP-3 (`@selfheal_guard` & `DriftDeduplicator`)**: Added sync/async runtime decorator with optional in-memory payload normalization (`normalize=True`), `compute_drift_fingerprint`, and thread-safe LRU + single-flight `DriftDeduplicator`.
- **EXP-4 (Nested & List Synthesis)**: Extended `libcst` patch synthesis to support nested sub-models and list-wrapped/coerced fields.
- **EXP-5 (TypeScript/Zod Emitter)**: Added `synthesize_zod_preprocessor` emitting `normalize<ModelName>Drift` and `createSelfHealZodAdapter` (`z.preprocess` wrapper).
- **Wave 3 (Hosted Webhook Server & Hardening)**: Added `python -m selfheal_api.server` with `/webhooks/drift` and `/healthz` endpoints and a Cloud Run `Dockerfile`.
- **Milestone 4 (Ledger, Notifications, OpenAPI 3.1 & Drift Console)**: Added persistent zero-PII SQLite Drift Event Ledger (`src/selfheal_api/ledger.py`), zero-PII Slack/webhook notifier (`src/selfheal_api/notifier.py`), OpenAPI 3.1 `x-selfheal-aliases` emitter (`emit_openapi_schema_patch`, `emit_openapi_document`), and XSS-safe operator Drift Console (`GET /dashboard`, `GET /api/v1/events`, `GET /api/v1/summary`).
- **Milestone 5 (ASGI/WSGI Middleware, Auto-Remediation & OpenAPI CLI)**: Added zero-dependency `SelfHealASGIMiddleware`, `SelfHealWSGIMiddleware`, and `RouteBinding` (`src/selfheal_api/middleware.py`), fail-closed `RemediationPipeline` (`src/selfheal_api/remediation.py`), and `--openapi` support in `selfheal heal`.
- **Milestone 6 (Background Worker, Replay Harness & Public SDK)**: Added bounded non-blocking `RemediationQueue` background worker, zero-PII `run_replay_corpus` regression harness, public `selfheal_api` SDK `__all__` exports with `__version__ = "0.2.0"`, and `docs/adr/006-background-remediation-worker-and-replay-harness.md`.

## [0.1.0] - 2026-11-25

### Added
- **Sentinel (`sentinel.detector`)**: Strict Pydantic v2 drift detection (`detect_drift`) with zero-PII `PrimitiveKind` scalar redaction and candidate path matching.
- **Synthesizer (`synthesizer.patcher`)**: `libcst` AST transformation (`synthesize_patch`) injecting typed `@model_validator(mode="before")` normalizers and `verify_patch` sandbox gate.
- **40-Mutation Benchmark Suite**: Deterministic evaluation suite across Stripe, HubSpot, and Weather API drift scenarios (`tests/test_drift_and_patch.py`), achieving 100.0% recall (40/40) and 100.0% verification pass rate (40/40).
- **Architecture & Threat Model**: Added `docs/adr/001-redacted-ast-synthesis-and-sandbox-gate.md` and `docs/THREAT_MODEL.md`.
