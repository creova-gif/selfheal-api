# TODOS

## Remaining External-Credential Integrations (Wave 3+)

> **Note:** The core Hosted Multi-Tenant Webhook Server (`python -m selfheal_api.server`, `POST /webhooks/drift`, `GET /healthz`) and Cloud Run `Dockerfile` are now implemented. The items below remain deferred until external SaaS credentials (Stripe, GitHub App OAuth) are provisioned.

### Stripe SaaS Billing Portal & GitHub App OAuth Installation Flow
- **What:** Stripe subscription billing portal (`$49-$199/mo`) and GitHub App OAuth installation flow paired with the Cloud Run hosted webhook receiver (`org_id` tenant isolation, GCP Secret Manager + OIDC).
- **Why:** Allows SaaS teams to self-serve subscribe and connect repositories via OAuth/GitHub App installation without configuring GitHub Actions or `gh` CLI tokens locally.
- **Pros:** Unlocks recurring SaaS monetization (`$49-$199/mo`) and centralized drift dashboards across 5-30 integrations.
- **Cons:** Requires live Stripe API keys/webhook secrets, registered GitHub App OAuth credentials, and GCP Secret Manager + OIDC setup.
- **Context:** Originally deferred during the 2026-11-25 CEO Plan Review (`D8`, `PROJECT_SPEC.md` Section 3 & Section 7) so the open-source MIT Python SDK, `selfheal` CLI (`gh pr create`), `@selfheal_guard` runtime, and Cloud Run webhook server ship cleanly first.
- **Effort estimate:** Human: ~1 week (L) | CC+gstack: ~2 hours (M)
- **Priority:** P2
- **Depends on / blocked by:** External Stripe + GitHub App OAuth credentials and first design-partner validation (`TASKS.md` Wave 3).
