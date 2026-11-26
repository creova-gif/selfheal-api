# ADR-004: Drift Ledger, Notifications, OpenAPI Emitter, and Drift Console
- **Status:** Accepted
- **Owner:** Justin Mafie

## Context
With the multi-tenant webhook server (ADR-002) and Stripe/GitHub App control plane (ADR-003) in place, `selfheal-api` needs persistent telemetry, proactive alerting, API specification export, and an operator-facing UI to complete the Milestone 4 product loop:
1. **Persistent Drift Ledger:** In-memory counters do not survive Cloud Run container restarts or support historical queries (`GET /api/v1/events`, `GET /api/v1/summary`).
2. **Proactive Drift Notifications:** Teams need immediate, zero-PII alerts in Slack or generic webhook endpoints when schema drift is detected or a remediation PR is opened.
3. **OpenAPI 3.1 Interoperability:** Synthesized Pydantic field aliases should be exportable as OpenAPI 3.1 schemas with `x-selfheal-aliases` metadata so downstream API gateways, SDK generators, and documentation stay synchronized.
4. **Operator Drift Console:** Tenants need a lightweight, zero-dependency HTML5 dashboard (`GET /dashboard`) to inspect plan quotas, drift metrics, and recent redacted events without requiring a separate SPA build pipeline.

## Decision
1. **SQLite WAL Zero-PII Drift Event Ledger (`src/selfheal_api/ledger.py`):**
   - Store redacted drift event metadata (organization ID, model name, structural fingerprint, drift status, missing/extra field names, PR status/URL, and UTC timestamp) in SQLite configured with `PRAGMA journal_mode=WAL` and `PRAGMA synchronous=NORMAL` for concurrent read/write safety using Python 3.12 stdlib `sqlite3`.
   - Enforce the zero-PII invariant at the storage boundary: raw payload values are never persisted—only structural field names, types, and hashes.
   - Expose per-tenant summary aggregations (`total_events`, `drifted_events`, `unique_fingerprints`, `prs_opened`) and paginated recent event queries.
2. **Slack / Webhook Alert Formatter (`src/selfheal_api/notifier.py`):**
   - Format drift notifications into structured Slack Block Kit payloads and generic JSON webhook payloads containing only redacted structural metadata (org ID, model name, fingerprint, field diffs, and PR link).
   - Deliver alerts via stdlib `urllib.request` with bounded timeouts and explicit error handling so notification failures never crash the drift ingestion path.
3. **OpenAPI 3.1 `x-selfheal-aliases` Emitter (`src/synthesizer/openapi_emitter.py`):**
   - Generate OpenAPI 3.1 (`"openapi": "3.1.0"`) component schemas and schema diffs from Pydantic models and synthesized alias mappings.
   - Attach an `x-selfheal-aliases` vendor extension mapping canonical field names to their observed upstream alias names, preserving full compatibility with standard OpenAPI 3.1 tooling.
4. **XSS-Safe HTML5 Drift Console (`src/selfheal_api/dashboard.py`):**
   - Define an immutable `DashboardSnapshot(BaseModel, frozen=True)` view model encapsulating org ID, plan tier, rate/LLM quota limits, aggregate drift counters, and recent redacted event records.
   - Render a self-contained, responsive HTML5 + inline CSS console (`render_dashboard_html`) with zero external asset dependencies and no inline JavaScript.
   - Escape every dynamic string (including `org_id`, `plan`, `model_name`, `fingerprint`, field names, and PR metadata) using `html.escape(..., quote=True)` to prevent reflected or stored XSS.

## Alternatives Considered
- **External Postgres / Cloud SQL + React SPA:** Rejected for Milestone 4 to avoid mandatory external database provisioning and Node.js build tooling in the core Python container, while keeping the ledger and snapshot interfaces cleanly separable for future external DB backends.
- **Third-Party Templating Engines (Jinja2) or Slack SDKs:** Rejected to keep runtime dependencies minimal (`pydantic` + `libcst` only) and avoid template auto-escaping misconfiguration risks by enforcing explicit `html.escape(..., quote=True)` in pure Python.

## Consequences & Failure Modes
- **Single-Node SQLite Durability:** SQLite WAL provides crash-safe local persistence and concurrent readers, but multi-instance horizontal scaling on Cloud Run still requires a shared database backend (e.g., Cloud SQL/Postgres or Litestream replication) behind the same ledger interface.
- **Webhook Delivery Best-Effort:** Outbound Slack/webhook HTTP calls are subject to network latency or endpoint downtime; timeouts and non-2xx responses must be logged and surfaced without blocking webhook ingestion.
- **Strict Escaping Discipline:** Any future fields added to `DashboardSnapshot` or `recent_events` must pass through `_esc()` (`html.escape(..., quote=True)`) to preserve the XSS-free guarantee.
