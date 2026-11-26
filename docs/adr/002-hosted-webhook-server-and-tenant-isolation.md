# ADR-002: Hosted Webhook Server and Multi-Tenant Isolation
- **Status:** Accepted
- **Owner:** Justin Mafie

## Context
`selfheal-api` needs a hosted HTTP webhook server capable of running on GCP Cloud Run (`PORT=8080`) to accept schema-drift reports from multiple organizations (`org_id`), verify webhook authenticity, enforce per-tenant rate limits and LLM call budgets, and return redacted drift diagnostics plus optional Zod TypeScript preprocessors—without adding new third-party runtime dependencies or leaking state/PII across tenants.

## Decision
1. **Stdlib Threaded HTTP Server:** Implement `src/selfheal_api/server.py` using Python 3.12's `http.server.ThreadingHTTPServer` and `BaseHTTPRequestHandler` (`SelfHealRequestHandler`), paired with Pydantic v2 (`TenantConfig`, `ServerConfig`, `DriftWebhookRequest`).
   - `GET /healthz` returns `200 OK` with `{"status": "ok", "version": "0.2.0"}`.
   - `POST /webhooks/drift` requires a valid `X-SelfHeal-Org` header, validates request size (`max_body_bytes`), verifies HMAC-SHA256 signatures, enforces per-tenant rate limits, runs zero-PII `detect_drift`, and returns redacted `DriftIssue` objects and optional `zod_adapter` TypeScript output.
2. **HMAC-SHA256 Authentication:** `verify_webhook_signature(secret, body, signature_header)` validates `X-Hub-Signature-256` (or `X-SelfHeal-Signature`) using `hmac.compare_digest` against `sha256=<hex>`, returning `401 Unauthorized` on missing or invalid signatures when `webhook_secret` is configured.
3. **Strict Per-Tenant Isolation (`TenantRegistry`):** `TenantRegistry` maintains a thread-safe mapping from `org_id` to an isolated `TenantState` containing:
   - Its own `DriftDeduplicator` (never sharing drift fingerprints or cached `PatchPlan`s across organizations).
   - Its own thread-safe `TokenBucketRateLimiter` (`max_requests_per_minute`, returning `429 Too Many Requests` when exhausted).
   - Its own optional `AnthropicSchemaMappingProvider(max_calls=max_llm_calls_per_tenant)` so one tenant cannot exhaust another tenant's LLM budget.
4. **Cloud Run Packaging:** Provide a multi-stage `python:3.12-slim` `Dockerfile` that builds into `/opt/venv`, drops privileges to a non-root `appuser`, sets `PORT=8080`, and runs `python -m selfheal_api.server`.

## Alternatives Considered
- **FastAPI / Uvicorn:** Rejected for now to avoid introducing external ASGI/web framework dependencies and keep the container image and supply-chain surface minimal.
- **Shared global `DriftDeduplicator` / rate limiter:** Rejected because cross-tenant cache hits could leak `PatchPlan` metadata or allow noisy-neighbor rate/LLM budget exhaustion.

## Consequences & Failure Modes
- **In-memory per-instance state:** Rate limits and deduplication caches are scoped to each server process; under multi-instance Cloud Run autoscaling, limits apply per instance unless backed by an external store in a future ADR.
- **Fail-closed request handling:** Missing/invalid `X-SelfHeal-Org` (`400`), unknown tenant when `allow_unregistered_tenants=False` (`403`), invalid/missing signature (`401`), rate limit exceeded (`429`), oversized body (`413`), and unknown model (`404`) all fail closed without invoking `detect_drift` or logging raw payloads.
