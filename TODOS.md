# TODOS

## Deferred from CEO Plan Review (2026-11-25, D8)

### Hosted Multi-Tenant GitHub App & GCP Cloud Run Billing Service
- **What:** Multi-tenant hosted webhook receiver on GCP Cloud Run paired with a GitHub App (`$49-$199/mo`) for zero-CLI PR automation and `org_id` tenant isolation.
- **Why:** Allows SaaS teams to connect repositories via OAuth/GitHub App installation without configuring GitHub Actions or `gh` CLI tokens locally.
- **Pros:** Unlocks recurring SaaS monetization (`$49-$199/mo`) and centralized drift dashboards across 5-30 integrations.
- **Cons:** Introduces multi-tenant `org_id` isolation, GCP Secret Manager + OIDC setup, GitHub App webhook signature verification, and Stripe billing maintenance.
- **Context:** Deferred during the 2026-11-25 CEO Plan Review (`D8`, `PROJECT_SPEC.md` Section 3 & Section 7) so the open-source MIT Python SDK, `selfheal` CLI (`gh pr create`), and `@selfheal_guard` runtime ship cleanly first.
- **Effort estimate:** Human: ~2 weeks (XL) | CC+gstack: ~3 hours (L)
- **Priority:** P2
- **Depends on / blocked by:** Completion of Wave 2 SDK & CLI (`T1`–`T5` in `PROJECT_SPEC.md`) and first design-partner validation (`TASKS.md` Wave 3).
