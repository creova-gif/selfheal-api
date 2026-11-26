# ADR-003: Stripe Billing and GitHub App Control Plane
- **Status:** Accepted
- **Owner:** Justin Mafie

## Context
Following ADR-002's hosted multi-tenant webhook server, `selfheal-api` requires a commercial control plane and production Cloud Run deployment configuration. Specifically, the service must:
1. Verify and process Stripe billing webhooks to enforce per-organization subscription entitlements without adding the `stripe` SDK to the runtime dependency graph.
2. Verify and track GitHub App installation webhooks so organizations can onboard repositories without static personal access tokens.
3. Deploy cleanly to GCP Cloud Run with Secret Manager bindings for all webhook signing secrets and LLM credentials.

## Decision
1. **Zero-Dependency Stripe Webhook Verification:**
   - Parse the `Stripe-Signature` header (`t=<unix_timestamp>,v1=<hex_hmac>`) using Python 3.12 stdlib (`hmac`, `hashlib`, `time`).
   - Compute `HMAC-SHA256` over `f"{timestamp}.{raw_body}"` using `STRIPE_WEBHOOK_SECRET`, verify with `hmac.compare_digest`, and enforce a bounded timestamp tolerance window to reject replayed payloads (`401 Unauthorized`).
2. **Thread-Safe `BillingStore`:**
   - Maintain a thread-safe per-tenant (`org_id`) billing and entitlement store (`BillingStore`) keyed by organization and Stripe customer/subscription identifiers.
   - Update entitlement state idempotently from Stripe lifecycle events (such as `checkout.session.completed`, `customer.subscription.updated`, `customer.subscription.deleted`, and `invoice.payment_failed`), enabling fail-closed quota and plan enforcement alongside `TenantRegistry`.
3. **GitHub App `InstallationRegistry`:**
   - Authenticate incoming GitHub App webhook events via `X-Hub-Signature-256` using `GITHUB_APP_WEBHOOK_SECRET` and `hmac.compare_digest`.
   - Track installation lifecycle (`installation`, `installation_repositories`) in a thread-safe `InstallationRegistry` mapping installation IDs and organizations to authorized repositories.
4. **Cloud Run Manifest & Secret Manager Bindings (`deploy/cloudrun.yaml`):**
   - Define a Knative `serving.knative.dev/v1` `Service` manifest targeting `selfheal-api:0.2.0` on `containerPort: 8080` with `containerConcurrency: 80` and resource limits `cpu: "1"`, `memory: 512Mi`.
   - Configure both `startupProbe` and `livenessProbe` HTTP checks on `GET /healthz` (port `8080`).
   - Bind `SELFHEAL_WEBHOOK_SECRET`, `STRIPE_WEBHOOK_SECRET`, `GITHUB_APP_WEBHOOK_SECRET`, and `ANTHROPIC_API_KEY` via GCP Secret Manager `secretKeyRef` (`key: latest`) rather than `.env` files or container-baked environment variables.

## Alternatives Considered
- **Official `stripe` and `PyGithub` SDKs:** Rejected to preserve minimal container size, fast Cloud Run cold starts, and a zero-extra-dependency webhook verification path.
- **Static GitHub Personal Access Tokens (PATs):** Rejected because PATs lack fine-grained per-installation repository scoping and clean org-level revocation.
- **External Persistent Store (Cloud SQL / Firestore) in v0.2.0:** Deferred; in-memory thread-safe `BillingStore` and `InstallationRegistry` match `TenantRegistry` (ADR-002) and can be backed by a durable store behind the same interface when multi-instance state sharing is required.

## Consequences & Failure Modes
- **Per-instance state under autoscaling:** As with `TenantRegistry` in ADR-002, in-memory `BillingStore` and `InstallationRegistry` state is scoped to each Cloud Run instance; horizontal scaling requires external persistence or sticky routing in a future ADR.
- **Fail-closed secret & signature handling:** Missing or mismatched `STRIPE_WEBHOOK_SECRET` or `GITHUB_APP_WEBHOOK_SECRET` rejects requests (`401`) rather than accepting unverified webhooks.
- **Secret rotation:** Binding `key: latest` in Secret Manager allows zero-code secret rotation, though running instances must be recycled (new Cloud Run revision or instance restart) to pick up rotated environment values.
