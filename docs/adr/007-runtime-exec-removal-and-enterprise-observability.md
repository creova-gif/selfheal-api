# ADR-007: Zero-Exec Runtime Patch Application, Pipeline Concurrency Lock, and Enterprise Observability
- **Status:** Accepted
- **Owner:** Justin Mafie

## Context
As `selfheal-api` transitions from early prototype middleware to enterprise production deployments, four hardening gaps needed to be addressed:
1. **Runtime `exec()` Removal:** Applying a `PatchPlan` in-process at request or replay time previously relied on dynamic code execution (`exec()`), which introduces security audit friction, static-analysis warnings, and unnecessary runtime overhead when normalizing drifted payloads.
2. **Concurrent Remediation Safety:** `RemediationPipeline` performs file writes, shadow `mypy --strict` checks, and `pytest` verification gates against target model files. Concurrent invocations on the same repository/model path risk race conditions and interleaved rollback corruption.
3. **Production Health, Tracing, and Metrics:** Enterprise orchestrators (Kubernetes, Cloud Run, Prometheus) require readiness probes (`/readyz`), request correlation (`X-Request-Id`), and Prometheus-compatible metrics endpoints rather than a single liveness check (`/healthz`).
4. **Operator Environment Diagnostics:** Teams onboarding `selfheal` CLI need a fast, zero-secret diagnostic command (`selfheal doctor`) to verify Python >= 3.12, `pydantic`, `libcst`, toolchain binaries (`ruff`, `mypy`, `pytest`, `git`, `gh`), and `ANTHROPIC_API_KEY` presence.

## Decision
1. **Pure-Python `apply_patch_plan_in_memory` Refactor:**
   - Replace runtime `exec()` evaluation with a pure-Python `apply_patch_plan_in_memory` helper that applies `PatchPlan` field renames, alias mappings, and safe type coercions directly to payload dictionaries in memory without compiling or executing synthesized Python source at request time.
2. **`RemediationPipeline` Concurrency Lock:**
   - Guard `RemediationPipeline` execution with a synchronization lock so concurrent remediation runs serialize cleanly, preventing overlapping atomic file writes or rollback races on shared model files.
3. **Enterprise Observability (`/readyz`, `X-Request-Id`, Prometheus Metrics):**
   - Expose `/readyz` readiness probing alongside `/healthz` liveness checks.
   - Propagate or generate `X-Request-Id` correlation headers across server responses and structured logs.
   - Expose Prometheus text-format metrics for drift detection, remediation queue depth/outcomes, and HTTP request telemetry.
4. **Zero-Secret `selfheal doctor` CLI Subcommand:**
   - Add `selfheal doctor` (with optional `--json` output) to `selfheal_api.cli` to verify Python `>= 3.12`, `pydantic` and `libcst` availability/versions, `ruff`/`mypy`/`pytest`/`git`/`gh` binary presence via `shutil.which`, and boolean `ANTHROPIC_API_KEY` configuration without ever printing secret values.
   - Exit `0` when required runtime dependencies (`python >= 3.12`, `pydantic`, `libcst`) are healthy and `1` otherwise.

## Alternatives Considered
- **Sandboxed `exec()` with Restricted Globals:** Rejected because even restricted `exec()` triggers enterprise SAST/security review blockers and is unnecessary when `PatchPlan` operations are declarative data transformations.
- **Process-Level File Locks Only (`fcntl`):** Rejected as the sole mechanism because in-process `RemediationQueue` workers share threads within a single process where an in-process lock is portable across platforms and simpler to reason about.
- **External OpenTelemetry Collector Requirement:** Rejected to keep the core server zero-dependency; `X-Request-Id` and Prometheus exposition provide immediate out-of-the-box observability.

## Consequences & Failure Modes
- **Declarative Transform Scope:** `apply_patch_plan_in_memory` supports declarative `PatchPlan` transformations; arbitrary custom Python validators still require offline CST synthesis (`selfheal heal`) and CI verification.
- **Serialized Pipeline Throughput:** The `RemediationPipeline` concurrency lock serializes active remediation gates, trading parallel `mypy`/`pytest` throughput for deterministic file integrity.
- **Optional Tooling Warnings:** `selfheal doctor` reports missing optional binaries (`ruff`, `mypy`, `pytest`, `git`, `gh`) or unset `ANTHROPIC_API_KEY` as informational status while gating exit code `0`/`1` strictly on required runtime dependencies (`python >= 3.12`, `pydantic`, `libcst`).
