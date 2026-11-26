# ADR-008: Durable Control-Plane Persistence, CI Matrix, and Performance Gates
- **Status:** Accepted
- **Owner:** Justin Mafie

## Context
As `selfheal-api` moves from single-process prototype hosting to resilient production deployments, three operational gaps remained:
1. **Ephemeral Control-Plane State:** `BillingStore` (per-org subscription tiers, Stripe customer/subscription bindings, and bounded Stripe webhook event deduplication) and `InstallationRegistry` (GitHub App `org_id` ↔ `installation_id` mappings and repository lists) were held purely in memory. Container restarts or Cloud Run scale-to-zero events wiped billing entitlements and GitHub installation state.
2. **Unified Persistence Configuration:** `DriftLedger` already honored `SELFHEAL_DB_PATH` (defaulting to `:memory:`). Requiring separate database paths or external services for billing and installation state would complicate container volume mounts and operational configuration.
3. **Runtime Compatibility & Regression Gates:** Production deployments target both Python 3.12 and 3.13, and hot-path drift detection / normalization must remain bounded in latency as features are added.

## Decision
1. **SQLite WAL Persistence for `BillingStore` & `InstallationRegistry`:**
   - Back `BillingStore` and `InstallationRegistry` with SQLite persistence using Write-Ahead Logging (`PRAGMA journal_mode=WAL`) and `threading.Lock` synchronization, preserving `:memory:` as the default for isolated tests.
   - Persist subscription state, bounded processed-event deduplication, and GitHub App installation records/repositories atomically across process restarts.
2. **Shared `SELFHEAL_DB_PATH` Control-Plane Durability:**
   - Share `SELFHEAL_DB_PATH` across `DriftLedger`, `BillingStore`, and `InstallationRegistry` in `selfheal_api.server` so a single mounted SQLite database provides durable control-plane state without external database dependencies.
3. **Python 3.12/3.13 CI Matrix & Performance Benchmark Gates:**
   - Configure `.github/workflows/ci.yml` with `strategy.matrix.python-version: ["3.12", "3.13"]` and `fail-fast: false`.
   - Run `pip install -e ".[dev]" && ruff check src tests && mypy --strict src tests && pytest -v && selfheal doctor --json` on every matrix leg, including deterministic performance benchmark gates in `pytest`.

## Alternatives Considered
- **External PostgreSQL or Redis:** Rejected as the default control-plane backend to keep `selfheal-api` zero-dependency and self-contained for single-container Cloud Run/Docker deployments and offline CI.
- **Separate SQLite Database Files per Store:** Rejected to avoid multiple volume-mount/env-var configurations and to keep control-plane backups and state management unified under `SELFHEAL_DB_PATH`.
- **Testing Only Python 3.12 in CI:** Rejected because Python 3.13 typing and stdlib changes must be continuously verified alongside 3.12.

## Consequences & Failure Modes
- **Single-Writer SQLite Topology:** SQLite WAL supports concurrent reads and serialized writes within a single container/host; horizontal multi-writer replicas would require swapping the store backend to an external networked database.
- **Persistent Volume Requirement:** When `SELFHEAL_DB_PATH` points to a file path in production, operators must mount persistent storage at that path; otherwise state remains ephemeral across container replacement.
- **CI Matrix Cost & Visibility:** Running the full suite on both Python 3.12 and 3.13 increases CI compute usage, while `fail-fast: false` ensures compatibility issues on either version are always surfaced independently.
