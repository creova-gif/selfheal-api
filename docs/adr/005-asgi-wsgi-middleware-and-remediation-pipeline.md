# ADR-005: Zero-Dependency ASGI/WSGI Middleware and Fail-Closed Remediation Pipeline
- **Status:** Accepted
- **Owner:** Justin Mafie

## Context
Up through Milestone 4, `selfheal-api` provided `@selfheal_guard` for Python call sites, the `selfheal` CLI for offline inspection/patching, and a hosted multi-tenant webhook server with SQLite ledger, Slack/webhook alerting, OpenAPI 3.1 emission, and an operator Drift Console. To support drop-in runtime protection for existing web services and end-to-end automated remediation in Milestone 5, two capabilities are needed:
1. **Framework-Agnostic HTTP Middleware:** Teams running FastAPI, Starlette, Litestar (ASGI) or Flask, Django (WSGI) need zero-config request/response drift detection without adding Starlette, FastAPI, or Flask as hard runtime dependencies of `selfheal-api`.
2. **Fail-Closed Automated Remediation Pipeline:** When drift is detected, teams need a composable `RemediationPipeline` that orchestrates detection, LibCST patch synthesis, sandbox verification (`verify_patch`), `mypy --strict --shadow-file`, `pytest`, and artifact emission (patched model, Zod preprocessor, OpenAPI 3.1 schema, or GitHub PR) with strict fail-closed rollback guarantees.

## Decision
1. **Zero-Dependency ASGI & WSGI Middleware (`SelfHealASGIMiddleware`, `SelfHealWSGIMiddleware`):**
   - Implement pure-protocol ASGI 3.0 (`__call__(scope, receive, send)`) and PEP 3333 WSGI (`__call__(environ, start_response)`) wrappers using only Python 3.12 stdlib and `pydantic`.
   - Inspect JSON payloads against registered `BaseModel` schemas on configured routes/methods, emitting redacted, deduplicated `DriftReport` events without mutating or leaking PII/PHI.
   - Support bounded payload inspection and non-blocking/fail-safe error handling so middleware inspection errors never break application traffic.
2. **Fail-Closed `RemediationPipeline`:**
   - Encapsulate the end-to-end remediation workflow (detect -> synthesize -> `verify_patch` -> `mypy --strict` -> `pytest` -> emit/PR) behind a deterministic pipeline interface.
   - Enforce fail-closed semantics: any failure in `verify_patch`, `mypy --strict`, `pytest`, or PR creation aborts the pipeline and rolls back any temporary writes (patched model, Zod preprocessor, OpenAPI 3.1 JSON, and git branch state) unless explicitly persisted.
3. **CLI `--openapi` Integration (`src/selfheal_api/cli.py`):**
   - Expose `--openapi <path>` on `selfheal heal` to emit OpenAPI 3.1.0 `components.schemas` JSON with `x-selfheal-aliases` via `emit_openapi_schema_patch` and `emit_openapi_document`, participating in the same atomic write and `--open-pr` rollback lifecycle.

## Alternatives Considered
- **Starlette `BaseHTTPMiddleware` / Flask Extension:** Rejected because it would couple `selfheal-api` to specific web framework packages and versions, violating the minimal-dependency core (`pydantic` + `libcst`).
- **Fail-Open Auto-Patching:** Rejected because writing unverified or type-unsafe CST patches to production repositories risks silent data corruption or broken builds.

## Consequences & Failure Modes
- **Request Body Buffering:** Inspecting JSON request bodies in ASGI/WSGI requires buffering the payload up to a configured byte limit before forwarding to the downstream app; oversized or non-JSON payloads are safely bypassed.
- **Gate Latency in Remediation:** Running `mypy --strict --shadow-file` and `pytest` makes `RemediationPipeline` unsuitable for synchronous request-path execution; it must run asynchronously (CI, CLI, or background worker) while middleware handles sub-millisecond runtime detection.
- **Strict Rollback Invariant:** Any new artifact emitters added to `selfheal heal` or `RemediationPipeline` must use `_atomic_write` and register pre-existence checks for rollback cleanup on gate or PR failure.
