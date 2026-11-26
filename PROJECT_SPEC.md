# SelfHealAPI: Self-Maintaining APIs & Autonomous Schema-Drift Engine

- **Model:** Open-Source MIT SDK + Hosted GitHub App ($49-$199/mo)
- **Thesis Alignment:** #10 Self-Maintaining APIs, #11 Cloud for Small Software
- **Full Google Doc Spec:** [SelfHealAPI: Self-Maintaining APIs & Autonomous Schema-Drift Engine](https://docs.google.com/document/d/11etng86w_E0jCCEJ3lLjUW7L4yODKeIf4-yIe7J0EbY/edit?usp=drivesdk&ouid=110227624285917376281)
- **Portfolio Strategy:** [00 - Startup & Open-Source Cross-Project Strategy](https://docs.google.com/document/d/12Wpg5mMGnjnl2uhMBTOR0QqRzQj8kwfzTLpvLoRuB5Q/edit?usp=drivesdk&ouid=110227624285917376281)

## 1. Goal & Problem
Detect third-party API payload drift in real time, synthesize PII-redacted AST adapter patches, and open test-verified GitHub PRs.

## 2. Target Customer (ICP) & Monetization
Backend/platform teams at SaaS startups maintaining 5-30 external REST/GraphQL/webhook integrations.

## 3. Tech Stack
Python 3.12 (Pydantic v2, libcst), TypeScript (Zod), Anthropic Claude, GitHub CLI (`gh pr create`) / Actions. *(Note: Multi-tenant Hosted GitHub App billing server & GCP Cloud Run deployment wrapper are deferred to `TODOS.md` per D8; this repository delivers the complete open-source MIT SDK, CLI, and `@selfheal_guard` runtime).*

## 4. Evaluation & Acceptance Thresholds
100% drift detection recall; >= 85% auto-patch PR pass rate across 40 injected API mutations; < 2ms runtime check.

## 5. Key Architectural Decision (ADR-001)
Require 100% passing sandbox tests + mypy --strict before opening a PR; redact all payload values before LLM synthesis.

## 6. Day 1–5 Quickstart
- [x] Set up sentinel/, synthesizer/, and tests/ directories
- [x] Implement detect_drift(expected_model, raw_json) with redacted payload snapshots
- [x] Build 10-mutation test suite simulating Stripe/HubSpot/Weather API drift
- [x] Verify libcst/Claude patch passes pytest and mypy cleanly

## 7. CEO Plan Review Working Ledger & Reality Check

### Decision Ledger

| ID and owner | Contract and evidence | Current | Proposed | Status | Exact approval and scope |
|---|---|---|---|---|---|
| D1 (Step 0 Prerequisite) | Skip `/office-hours` and use `PROJECT_SPEC.md` + `ADR-001` (`PROJECT_SPEC.md:1-27`, `docs/adr/001-redacted-ast-synthesis-and-sandbox-gate.md:1-37`) | Standard CEO review against `PROJECT_SPEC.md` | A) Run `/office-hours` / B) Skip | approved | User selected `B) Skip — proceed with standard review` (D1) |
| D2 (Step 0E Mode) | Review posture selection (`PROJECT_SPEC.md:1-27`) | `SCOPE EXPANSION` | A) SCOPE EXPANSION / B) SELECTIVE EXPANSION / C) HOLD SCOPE / D) SCOPE REDUCTION | approved | User selected `A) SCOPE EXPANSION` (D2) |
| EXP-1 (Step 0G Scope) | `selfheal` CLI (`check`, `heal --diff --open-pr`) + Unified Diff & GitHub PR Emitter (`pyproject.toml:1-39`, `src/selfheal_api/__init__.py:1-38`) | Add `src/selfheal_api/cli.py`, `[project.scripts] selfheal`, unified diff output, and `gh pr create` helper + CLI tests | A) Add to this plan's scope (recommended) / B) Defer to TODOS.md / C) Skip | approved | User selected `A) Add to this plan's scope` (D3) |
| EXP-2 (Step 0G Scope) | Zero-PII Pluggable LLM Fallback Resolver (`SchemaMappingProvider`) for opaque field renames (`src/sentinel/detector.py:338-383`, `PROJECT_SPEC.md:15,21`) | Add `SchemaMappingProvider` protocol + Anthropic Claude HTTP adapter and offline test stub triggered only when heuristic rename confidence is below threshold, passing strictly `RedactedValue` + field types | A) Add to this plan's scope (recommended) / B) Defer to TODOS.md / C) Skip | approved | User selected `A) Add to this plan's scope` (D4) |
| EXP-3 (Step 0G Scope) | Drop-in Webhook Guard Decorator (`@selfheal_guard`) & Drift Fingerprint Deduplicator (`PROJECT_SPEC.md:9,12`, `src/sentinel/detector.py:423`) | Build `src/sentinel/guard.py` with `compute_drift_fingerprint(report)` and `@selfheal_guard` decorator for deduplicated real-time drift capture and optional runtime normalization | A) Add to this plan's scope (recommended) / B) Defer to TODOS.md / C) Skip | approved | User selected `A) Add to this plan's scope` (D5) |
| EXP-4 (Step 0G Scope) | Deep Nested `BaseModel` & Multi-Element List Drift Synthesis (`src/sentinel/detector.py:398-401`, `src/synthesizer/patcher.py:55-76`) | Upgrade `sentinel` to union `PrimitiveKind` across all list elements and upgrade `synthesizer` to route nested `issue.path` drift to child `BaseModel` classes in the same module | A) Add to this plan's scope (recommended) / B) Defer to TODOS.md / C) Skip | approved | User selected `A) Add to this plan's scope` (D6) |
| EXP-5 (Step 0G Scope) | TypeScript / Zod Cross-Language Adapter Emitter (`PROJECT_SPEC.md:15`, `src/synthesizer/patcher.py:45-76`) | Build `src/synthesizer/zod_emitter.py` (`synthesize_zod_preprocessor(plan)`) and expose `--zod` on `selfheal heal` to emit TypeScript `z.preprocess(...)` adapters alongside Python `libcst` patches | A) Add to this plan's scope (recommended) / B) Defer to TODOS.md / C) Skip | approved | User selected `A) Add to this plan's scope` (D7) |
| SPEC-REV-1 (Step 0H Spec Review) | Resolve 11 Spec Reviewer findings (`COMP-1..3`, `CONS-1..3`, `CLAR-1..4`, `SCOPE-1`, `FEAS-1..4`) across `PROJECT_SPEC.md` and `CEO_PLAN` (`PROJECT_SPEC.md:59-107`, `2026-11-25-selfheal-api-expansion.md:1-46`) | Apply all 11 spec-reviewer contract clarifications & defer Hosted Cloud Run/GitHub App to `TODOS.md` | A) Apply all 11 spec-reviewer contract clarifications & defer Hosted Cloud Run/GitHub App to `TODOS.md` (recommended) / B) Keep current spec with open reviewer concerns | approved | User selected `A) Apply all 11 spec-reviewer contract clarifications & defer Hosted Cloud Run/GitHub App to TODOS.md` (D8) |
| DOC-APP-1 (Step 0H Approval) | Approve amended working plan (`PROJECT_SPEC.md`) and CEO scope summary (`2026-11-25-selfheal-api-expansion.md`) to continue to Step 0I | Approved document versions (Iteration 1: 6.8/10, 11 findings fixed in D8; Iteration 2 score unavailable due to Vertex AI 429) | A) Approve these documents and continue to 0I (recommended) / B) Revise these documents / C) Pause this review | approved | User selected `A) Approve these documents and continue to 0I` (D9) |

**Approval readiness: PASS** — Checked ledger rows `D1`, `D2`, `EXP-1 (D3)`, `EXP-2 (D4)`, `EXP-3 (D5)`, `EXP-4 (D6)`, `EXP-5 (D7)`, `SPEC-REV-1 (D8)`, and `DOC-APP-1 (D9)`; all 9 rows are approved with exact scope and zero unresolved items.

### Step 0I: Temporal Interrogation (EXPANSION mode)

- **Effort Estimate (all 5 expansions):** Human team ~3.5 days | CC + gstack ~70 min.
```
  HOUR 1 (foundations):     EXP-4 list-container slicing (`loc` before first `int`) + `_classify_list_kinds` + `_type_compatible("list[...]")` + `build_patch_plan(..., root_model=None)` signature.
  HOUR 2-3 (core logic):   EXP-2 `SchemaMappingProvider` protocol (`^[A-Za-z0-9_-]{1,64}$` key filter, `_is_valid_redacted_kind`, `AnthropicSchemaMappingProvider` 5.0s timeout) + EXP-3 `DriftDeduplicator` pre-LLM SHA-256 fingerprint + `@selfheal_guard(normalize=...)` + EXP-5 `synthesize_zod_preprocessor`.
  HOUR 4-5 (integration):  EXP-1 `selfheal check` / `selfheal heal` CLI (`--shadow-file` mypy gate + pytest gate + `--open-pr` with `shell=False` and `timeout=30`).
  HOUR 6+ (polish/tests):  Deterministic unit/integration tests for prompt-injection key dropping, burst deduplication (`async def` + `asyncio.to_thread`), child `BaseModel` relative `source_path` + `model_rebuild(force=True)`, and TypeScript Zod output.
```

## 9. Deep Plan Review (Sections 1–11)

**Current scope:**
- **Mode:** `SCOPE EXPANSION` (D2), rationale: build the 10x end-to-end SelfHealAPI toolkit (CLI/PR bot, Zero-PII LLM fallback, `@selfheal_guard` + deduplicator, nested/list synthesis, TS/Zod emitter) over the green 16-test core.
- **Accepted:** `D1` (skip `/office-hours`), `D3` (EXP-1 CLI & PR emitter), `D4` (EXP-2 Zero-PII LLM fallback), `D5` (EXP-3 `@selfheal_guard` & deduplicator), `D6` (EXP-4 Nested `BaseModel` & list synthesis), `D7` (EXP-5 TS/Zod emitter), `D8` (11 spec-review fixes + defer Cloud Run/GitHub App), `D9` (approve Step 0H documents).
- **Deferred:** Hosted multi-tenant GitHub App & GCP Cloud Run billing server (`TODOS.md`, D8).
- **Rejected / Pending:** None.

### Section 1: Architecture Review
```
  +-----------------------+     +----------------------------+     +-----------------------------+
  | Webhook / CLI Input   | --> | sentinel.detector          | --> | DriftDeduplicator (EXP-3)   |
  | (raw JSON / dict)     |     | - strict=True validation   |     | - pre-LLM SHA-256 hash      |
  +-----------------------+     | - zero-PII PrimitiveKind   |     | - bounded LRU (max 1024)    |
                                | - heuristic path scoring   |     +--------------+--------------+
                                +-------------+--------------+                    | (unseen fp only)
                                              |                                   v
                                              |                    +-----------------------------+
                                              |                    | SchemaMappingProvider (EXP-2)|
                                              |                    | - RedactedValue + regex keys|
                                              |                    | - Anthropic 5s / Stub       |
                                              |                    +--------------+--------------+
                                              v                                   |
                                +----------------------------+                    |
                                | synthesizer.patcher (EXP-4)| <------------------+
                                | - PatchPlan IR             |
                                | - libcst CSTTransformer    |
                                | - child BaseModel routing  |
                                +-------------+--------------+
                                              |
                      +-----------------------+-----------------------+
                      v                                               v
        +----------------------------+                  +----------------------------+
        | Verification Gate (ADR-001)|                  | zod_emitter.py (EXP-5)     |
        | - verify_patch + rebuild   |                  | - normalize<Model>Drift    |
        | - mypy --shadow-file       |                  | - z.preprocess(...)        |
        | - pytest                   |                  +----------------------------+
        +-------------+--------------+
                      | (100% pass)
                      v
        +----------------------------+
        | selfheal_api.cli (EXP-1)   |
        | - unified diff / --write   |
        | - gh pr create (shell=False)|
        +----------------------------+
```
- **Component Boundaries & Coupling:** `sentinel` owns zero-PII boundary parsing, redaction, and deduplication; `synthesizer` consumes only `DriftReport`/`PatchPlan` (never raw payloads); `cli` orchestrates subprocess gates (`mypy --shadow-file`, `pytest`, `git`, `gh`).
- **Scaling & Single Points of Failure:** Heuristic path runs in `<0.05ms` per payload; `DriftDeduplicator` caps memory at 1024 fingerprints (`threading.Lock`) and gates LLM calls so 100x webhook bursts do not exhaust Anthropic rate limits or block the asyncio event loop.
- **Rollback Posture:** Generated patches are standard `@model_validator(mode='before')` methods in git commits; rollback is `git revert <sha>` (`< 1 min`).

### Section 2: Error & Rescue Map

| Method / Codepath | What Can Go Wrong | Exception Class | Rescued? | Rescue Action | User Sees |
|---|---|---|---|---|---|
| `cli.load_model` | Missing file, bad `file:Class` format, or import error | `FileNotFoundError`, `ValueError`, `ImportError` | Y | Print structured error to stderr; exit `2` | Clear CLI error message (exit 2) |
| `cli.heal` (`mypy`/`pytest` gate) | `mypy --strict` or `pytest` fails on patched model | `subprocess.CalledProcessError`, `subprocess.TimeoutExpired` | Y | Abort before `--write`/`--open-pr`; print stderr; exit `1` | Gate failure detail; repo untouched |
| `cli.heal --open-pr` | `git` dirty tree, missing `gh` CLI, or `gh` auth failure | `subprocess.CalledProcessError`, `FileNotFoundError`, `subprocess.TimeoutExpired` | Y | Print `gh`/`git` diagnostic; leave patch local; exit `2` | Explicit PR failure message |
| `AnthropicSchemaMappingProvider` | Network timeout (`>5s`), HTTP 429/5xx, malformed JSON, refusal | `urllib.error.URLError`, `TimeoutError`, `json.JSONDecodeError`, `KeyError` | Y | Log warning with model/field count; return `{}` | Graceful fallback to default coercion |
| `@selfheal_guard` (`on_drift`) | User callback raises exception | `Exception` (boundary isolation around user callback) | Y | Log exception via `selfheal_api` logger; continue handler | Webhook handler still returns 200 |
| `verify_patch` | CST syntax error, `exec` failure, `ValidationError` | `cst.ParserSyntaxError`, `ValidationError`, `TypeError` | Y | Return `VerificationResult(passed=False, error_detail=...)` | Explicit verification failure |

### Section 3: Security & Threat Model
- **Zero-PII Enforcement:** `_is_valid_redacted_kind` asserts every value sent to `SchemaMappingProvider` is a `PrimitiveKind` or `list[<PrimitiveKind>|...]`.
- **Prompt Injection Defense:** Candidate JSON keys containing newlines, control characters, or failing `^[A-Za-z0-9_-]{1,64}$` are dropped before prompt construction.
- **Subprocess Safety:** All CLI `mypy`, `pytest`, `git`, and `gh` invocations use `shell=False` with explicit `list[str]` arguments and hard `timeout` values (30s–60s).
- **Local Model Loading Boundary:** `selfheal check/heal --model` documents that loading a local `.py` file executes module-level Python code in the developer's environment (CLI-only, never exposed as an unauthenticated remote endpoint).

### Section 4: Data Flow & Interaction Edge Cases
```
  RAW PAYLOAD ---> detect_drift (strict=True) ---> pre-LLM Fingerprint ---> Seen in LRU?
       |                  |                               |                      |
   [invalid JSON]     [no drift]                    [unseen fp]              [seen fp]
       |                  |                               |                      |
       v                  v                               v                      v
  INVALID_JSON      return has_drift=False       LLM Fallback (async      skip LLM & on_drift
  (no crash)        (<0.05ms)                    via asyncio.to_thread)   (<0.05ms)
```
- **Shadow Paths:** `nil`/missing JSON returns `INVALID_JSON`; empty `{}`/`[]` returns `EMPTY_OBJECT`/`EMPTY_LIST`; oversized lists cap inspection at 256 elements; async `@selfheal_guard` offloads `AnthropicSchemaMappingProvider` via `asyncio.to_thread` so the event loop never stalls.

### Section 5: Code Quality Review
- Follows `AGENTS.md`: Python 3.12+, `mypy --strict`, `ruff check`, Pydantic v2 `ConfigDict(frozen=True)`, no bare `except Exception: pass` (user callback isolation logs via `logging.getLogger("selfheal_api")`).
- Keeps `build_patch_plan(report, root_model=None)` and `synthesize_patch(source_code, report, root_model=None)` backwards-compatible with existing 16 tests.

### Section 6: Test Review
- **Unit & Integration Tests:**
  - `tests/test_cli.py`: `selfheal check` exit codes (`0`/`1`/`2`), `selfheal heal --diff --zod` output, `mypy --shadow-file` + `pytest` gate rejection on broken patch, mocked `gh pr create` call.
  - `tests/test_llm_fallback.py`: Opaque rename resolution via `DeterministicMappingProvider`, prompt-injection key dropping (`^[A-Za-z0-9_-]{1,64}$`), zero-PII assertion, timeout/429/malformed JSON rescue.
  - `tests/test_guard.py`: Deterministic SHA-256 fingerprint, 1,000-event burst deduplication (`on_drift` called once), `async def` handler + `normalize=True` in-memory normalization, `on_drift` exception isolation.
  - `tests/test_nested_and_zod.py`: Heterogeneous list `PrimitiveKind` unioning (`list[<int>|<str>]`), `list[float]`/`list[bool]`/`list[int]` (excluding `bool`), 2-level nested `BaseModel` relative `source_path` patching + `model_rebuild(force=True)`, and TypeScript Zod `z.preprocess` output.

### Section 7: Performance Review
- Fast-path `detect_drift` remains `<0.05ms` median (`0.017ms` measured); `DriftDeduplicator` adds `<0.01ms` SHA-256 + `OrderedDict` lookup; list element inspection caps at 256 items (`O(1)` bounded).

### Section 8: Observability & Debuggability Review
- Structured logging on `logging.getLogger("selfheal_api")` for LLM fallback invocations/errors, `DriftDeduplicator` cache hits/misses, and `on_drift` callback exceptions (without ever logging raw payload values).

### Section 9: Deployment & Rollout Review
- Pure Python package (`pyproject.toml` `[project.scripts] selfheal`) with zero new runtime dependencies beyond `pydantic>=2.9` and `libcst>=1.4`. Rollback is `git revert`.

### Section 10: Long-Term Trajectory Review
- Reversibility: **5/5** (modular files `cli.py`, `llm_fallback.py`, `guard.py`, `zod_emitter.py`; `PatchPlan` IR cleanly decouples detection from Python/TS codegen).

### Section 11: Design & UX Review
- **SKIPPED (no UI scope)** — CLI, Python SDK, and TypeScript codegen only.

### Error & Rescue + Failure Modes Registry

| Codepath | Failure Mode | Rescued? | Test? | User Sees? | Logged? |
|---|---|---|---|---|---|
| `cli.heal` verification gate | `mypy --shadow-file` or `pytest` fails | Y | Y (`test_cli.py`) | Error on stderr, exit 1 | Y |
| `cli.heal --open-pr` | `git`/`gh` command fails or times out | Y | Y (`test_cli.py`) | Diagnostic on stderr, exit 2 | Y |
| `AnthropicSchemaMappingProvider` | Timeout / 429 / malformed JSON / refusal | Y | Y (`test_llm_fallback.py`) | Graceful `{}` fallback | Y |
| `@selfheal_guard` | `on_drift` callback raises | Y | Y (`test_guard.py`) | Handler still succeeds | Y |
| `verify_patch` | Invalid CST or `ValidationError` | Y | Y (`test_drift_and_patch.py`) | `VerificationResult(passed=False)` | N (returned) |

### NOT in Scope & Deferred Work
- **Deferred to `TODOS.md`:** Multi-tenant Hosted GitHub App billing server and GCP Cloud Run webhook receiver (`PROJECT_SPEC.md:3,15`, D8).

### What Already Exists
- `src/sentinel/detector.py` (`detect_drift`, `redact_payload`, `PrimitiveKind`, 100% recall).
- `src/synthesizer/patcher.py` (`build_patch_plan`, `synthesize_patch`, `verify_patch`, `libcst` transformer).
- `tests/test_drift_and_patch.py` (16 passing tests, 40-mutation benchmark).

### Dream State Delta
Moves SelfHealAPI from an in-memory Python library to an end-to-end CLI + PR bot, zero-PII Claude fallback, deduplicated webhook guard, nested-model/list synthesizer, and TypeScript/Zod adapter emitter — one step away from the 12-month hosted multi-tenant GitHub App control plane.

## Implementation Tasks
Synthesized from this review's findings. Each task derives from a specific finding above.

- [ ] **T1 (P1, human: ~6h / CC: ~15m)** — `sentinel` & `synthesizer` (EXP-4) — Implement `_classify_list_kinds` (256-item cap), `err["loc"]` list-index slicing, `list[str|int|float|bool]` coercion, backwards-compatible `root_model=None` nested `BaseModel` relative `source_path` routing, and `model_rebuild(force=True)`
  - Surfaced by: Section 1/4/5 & D6/D8 (`FEAS-1..3`, `COMP-3`, `CLAR-3`)
  - Files: `src/sentinel/detector.py`, `src/synthesizer/patcher.py`, `tests/test_nested_and_zod.py`
  - Verify: `.venv/bin/ruff check src tests && .venv/bin/mypy --strict src tests && .venv/bin/pytest -v`
- [ ] **T2 (P1, human: ~1d / CC: ~20m)** — `sentinel.llm_fallback` (EXP-2) — Implement `SchemaMappingProvider` protocol, `^[A-Za-z0-9_-]{1,64}$` key sanitization, `_is_valid_redacted_kind`, `AnthropicSchemaMappingProvider` (5.0s timeout + rescue), and `DeterministicMappingProvider`
  - Surfaced by: Section 2/3 & D4/D8 (`CONS-3`, `CLAR-1`)
  - Files: `src/sentinel/llm_fallback.py`, `src/sentinel/detector.py`, `src/sentinel/__init__.py`, `tests/test_llm_fallback.py`
  - Verify: `.venv/bin/ruff check src tests && .venv/bin/mypy --strict src tests && .venv/bin/pytest -v`
- [ ] **T3 (P1, human: ~4h / CC: ~15m)** — `sentinel.guard` (EXP-3) — Implement `compute_drift_fingerprint`, thread-safe bounded `DriftDeduplicator`, and `@selfheal_guard` (pre-LLM deduplication, `asyncio.to_thread`, `normalize: bool = False`, isolated `on_drift` logging)
  - Surfaced by: Section 1/2/4 & D5/D8 (`COMP-2`, `CONS-1`, `CLAR-2`)
  - Files: `src/sentinel/guard.py`, `src/sentinel/__init__.py`, `tests/test_guard.py`
  - Verify: `.venv/bin/ruff check src tests && .venv/bin/mypy --strict src tests && .venv/bin/pytest -v`
- [ ] **T4 (P2, human: ~4h / CC: ~10m)** — `synthesizer.zod_emitter` (EXP-5) — Implement `synthesize_zod_preprocessor(plan)` generating per-model `normalize<TargetModel>Drift` and `createSelfHealZodAdapter`
  - Surfaced by: Section 1/6 & D7/D8 (`CLAR-4`)
  - Files: `src/synthesizer/zod_emitter.py`, `src/synthesizer/__init__.py`, `tests/test_nested_and_zod.py`
  - Verify: `.venv/bin/ruff check src tests && .venv/bin/mypy --strict src tests && .venv/bin/pytest -v`
- [ ] **T5 (P1, human: ~4h / CC: ~15m)** — `selfheal_api.cli` (EXP-1) — Implement `selfheal check` and `selfheal heal` CLI (`--llm-fallback`, `--pytest-args`, `--diff`, `--write`, `--zod`, `--open-pr`, `--branch`, `mypy --strict --shadow-file` + `pytest` gate) and `[project.scripts]` entrypoint
  - Surfaced by: Section 2/3 & D3/D8 (`COMP-1`, `CONS-2`, `FEAS-4`)
  - Files: `src/selfheal_api/cli.py`, `src/selfheal_api/__init__.py`, `pyproject.toml`, `tests/test_cli.py`
  - Verify: `.venv/bin/ruff check src tests && .venv/bin/mypy --strict src tests && .venv/bin/pytest -v`

### Answered Decision (EXP-1 / D3)
Approved `A) Add to this plan's scope`: Build `src/selfheal_api/cli.py` + `[project.scripts] selfheal` with unified diff generation and guarded GitHub PR creation.

### Answered Decision (EXP-2 / D4)
Approved `A) Add to this plan's scope`: Add `SchemaMappingProvider` protocol + Anthropic Claude HTTP adapter and offline test stub triggered only when heuristic rename confidence is below threshold, passing strictly `RedactedValue` + field types.

### Answered Decision (EXP-3 / D5)
Approved `A) Add to this plan's scope`: Build `src/sentinel/guard.py` with `compute_drift_fingerprint(report)` and `@selfheal_guard` decorator for deduplicated real-time drift capture and optional runtime normalization.

### Answered Decision (EXP-4 / D6)
Approved `A) Add to this plan's scope`: Upgrade `sentinel` to union `PrimitiveKind` across all list elements and upgrade `synthesizer` to route nested `issue.path` drift to child `BaseModel` classes in the same module.

### Answered Decision (EXP-5 / D7)
Approved `A) Add to this plan's scope`: Build `src/synthesizer/zod_emitter.py` (`synthesize_zod_preprocessor(plan)`) and expose `--zod` on `selfheal heal` to emit TypeScript `z.preprocess(...)` adapters alongside Python `libcst` patches.

### Answered Decision (SPEC-REV-1 / D8)
Approved `A) Apply all 11 spec-reviewer contract clarifications & defer Hosted Cloud Run/GitHub App to TODOS.md`: Amended Section 8 with all 11 completeness, consistency, clarity, scope, and feasibility fixes and deferred the hosted `GCP Cloud Run` / `GitHub App` server to `TODOS.md`.

## 8. Expanded Architecture & Implementation Specification (EXP-1 through EXP-5)

### 8.1 EXP-1: `selfheal` CLI + Unified Diff & Guarded GitHub PR Emitter (`src/selfheal_api/cli.py`)
- **Entrypoint:** `[project.scripts] selfheal = "selfheal_api.cli:main"` in `pyproject.toml`.
- **Subcommands:**
  - `selfheal check --model <path/to/file.py:ClassName> --payload <path/to/payload.json> [--llm-fallback]`: Loads the target Pydantic `BaseModel` from the module path, runs `detect_drift(model_cls, raw_json, mapping_provider=...)`, prints the zero-PII `DriftReport` JSON to stdout, and exits `0` (no drift), `1` (drift detected), or `2` (invalid arguments / file / model load error).
  - `selfheal heal --model <path/to/file.py:ClassName> --payload <path/to/payload.json> [--canonical <path/to/canonical.json>] [--llm-fallback] [--pytest-args <args>] [--diff] [--write] [--zod <out.ts>] [--open-pr] [--branch <branch-name>]`:
    1. Runs `detect_drift(model_cls, raw_json, mapping_provider=...)` (activating `AnthropicSchemaMappingProvider` when `--llm-fallback` is passed).
    2. Synthesizes the Python CST patch via `synthesize_patch(source_code, report, root_model=model_cls)` and runs the 3-step hard verification gate (`ADR-001`) before touching the working tree:
       - In-memory CST syntax + runtime payload check via `verify_patch(patch_result, drifted_payload, canonical_payload)`.
       - Static type check via `subprocess.run([sys.executable, "-m", "mypy", "--strict", "--shadow-file", str(model_path), str(temp_patched_path), str(model_path)], check=False, timeout=30)` so relative imports and sibling modules resolve in the exact package context of `<model_path>` without mutating `<model_path>` (`FEAS-4`).
       - Test suite execution via `subprocess.run([sys.executable, "-m", "pytest", *pytest_args], check=False, timeout=60)` (`COMP-1`).
    3. If any of the 3 verification gate checks fails, prints the structured error to stderr and exits `1` without writing `<model_path>` or opening a PR.
    4. If `--diff` is passed, prints a unified diff (`difflib.unified_diff`) between `original_source` and `patched_source` to stdout.
    5. If `--zod <out.ts>` is passed, writes the TypeScript `z.preprocess(...)` adapter generated by `synthesize_zod_preprocessor(patch_result.plan)` to `<out.ts>`.
    6. If `--write` or `--open-pr` is passed (`--open-pr` implies `--write`, `CONS-2`), atomically writes `patched_source` to `<model_path>` via a temp file + `os.replace`.
    7. If `--open-pr` is passed, uses `--branch <branch-name>` (defaulting to `selfheal/<model-name-lower>-<short-fingerprint8>` when `--branch` is omitted, `CONS-2`), runs `git checkout -B <branch>`, `git add <model_path> [<out.ts>]`, `git commit -m ...`, and `gh pr create --title ... --body ...` via `subprocess.run(..., shell=False, timeout=30)`.

### 8.2 EXP-2: Zero-PII Pluggable LLM Fallback Resolver (`src/sentinel/llm_fallback.py`)
- **Interface (`SchemaMappingProvider` Protocol):**
  - `resolve_opaque_mappings(self, model_name: str, missing_fields: dict[str, str], extra_observed_kinds: dict[str, str]) -> dict[str, tuple[str | int, ...]]`
- **Key Path Format & Zero-PII Validation (`CLAR-1`, `CONS-3`):**
  - Both `missing_fields` (mapping dot-delimited missing field path -> expected type string, e.g. `{"billing.postal_code": "str"}`) and `extra_observed_kinds` (mapping dot-delimited extra candidate path -> observed kind string, e.g. `{"billing.zip_ref": "<int_str>", "raw_tags": "list[<str>]"}`) use dot-delimited path keys (`".".join(str(p) for p in path)`).
  - Every segment of every key in `extra_observed_kinds` must match `^[A-Za-z0-9_-]{1,64}$`; any candidate path with a segment failing this regex (such as control characters, newlines, or prompt-injection payloads) is dropped *before* constructing `extra_observed_kinds` (`CLAR-1`).
  - Every value in `extra_observed_kinds` is validated by `_is_valid_redacted_kind(val: str) -> bool`, which accepts either a direct `PrimitiveKind` enum member (`<str>`, `<int>`, etc.) or a composite `list[<PrimitiveKind>|...]` string whose pipe-separated inner tokens are all valid `PrimitiveKind` enum members (`CONS-3`). If any value fails this check, a `ValueError` is raised before any external call.
  - The returned `dict` maps a dot-delimited key from `missing_fields` to its matched `tuple[str | int, ...]` candidate path from `extra_candidates`.
- **Trigger Condition:**
  - In `detect_drift(expected_model, raw_json, mapping_provider: SchemaMappingProvider | None = None)`, heuristic scoring runs first (`<0.05ms`). Only if one or more `MISSING_FIELD` issues remain unmatched (`candidate_source_path is None`) AND `extra_candidates` remain unused AND `mapping_provider is not None`, `detect_drift` calls `mapping_provider.resolve_opaque_mappings(...)`.
  - Any returned candidate path is verified for exact membership in unused `extra_candidates` and `_type_compatible(expected_type, observed_kind)` before being attached to `DriftIssue.candidate_source_path`.
- **Implementations:**
  - `AnthropicSchemaMappingProvider`: Calls `https://api.anthropic.com/v1/messages` using `urllib.request` with a 5.0s timeout, `ANTHROPIC_API_KEY` from environment, and strict JSON response parsing; rescues `URLError`, `TimeoutError`, HTTP 429/5xx, malformed JSON, and LLM refusals by logging structured context and returning `{}` (graceful degradation to unmapped field default).
  - `DeterministicMappingProvider`: In-memory dictionary/callable provider for deterministic unit and integration testing without network calls.

### 8.3 EXP-3: Drop-in Webhook Guard Decorator & Drift Fingerprint Deduplicator (`src/sentinel/guard.py`)
- **Fingerprint Calculation (`compute_drift_fingerprint(report: DriftReport) -> str`):**
  - Computes a deterministic SHA-256 hex digest over `(report.model_name, sorted((i.kind, i.path, i.expected_type, i.observed_kind, i.candidate_source_path) for i in report.issues))`.
- **Deduplicating Guard (`DriftDeduplicator` & `@selfheal_guard`) (`CONS-1`, `COMP-2`, `CLAR-2`):**
  - `DriftDeduplicator(max_fingerprints: int = 1024)` maintains a thread-safe (`threading.Lock`) bounded `OrderedDict` LRU cache of seen drift fingerprints so memory is strictly bounded (`O(max_fingerprints)`).
  - `@selfheal_guard(model_cls, *, on_drift: Callable[[DriftReport, str], None] | None = None, mapping_provider: SchemaMappingProvider | None = None, deduplicator: DriftDeduplicator | None = None, normalize: bool = False)` wraps both synchronous (`def`) and asynchronous (`async def`) webhook handler functions/methods:
    1. **Payload extraction (`CLAR-2`):** Inspects `payload` keyword argument first; otherwise takes the first positional argument (or second positional argument when the first parameter of the wrapped callable is `self` or `cls`), accepting `str | bytes | dict[str, Any]`.
    2. **Fast-path heuristic detection & pre-LLM deduplication (`CONS-1`):** Always runs heuristic-only `detect_drift(model_cls, raw_payload, mapping_provider=None)` first (`<0.05ms`, zero network I/O).
    3. If `report.has_drift`:
       - Computes the pre-LLM structural fingerprint `pre_fp = compute_drift_fingerprint(report)`.
       - Checks `deduplicator.check_and_record(pre_fp)`:
         - **New unseen drift fingerprint:** If `mapping_provider is not None`, runs `detect_drift(model_cls, raw_payload, mapping_provider=mapping_provider)` (executing inside `await asyncio.to_thread(...)` when wrapping an `async def` handler so the event loop is never blocked, `CONS-1`), caches the resolved `PatchPlan` against `pre_fp` in `deduplicator`, and invokes `on_drift(enriched_report, compute_drift_fingerprint(enriched_report))` inside a `try ... except Exception` block that logs errors via `logging.getLogger("selfheal_api")` without crashing the webhook handler (`COMP-2`).
         - **Already-seen drift fingerprint:** Skips `mapping_provider` and skips `on_drift` (`<0.05ms` total overhead across 10,000 repeated webhooks).
       - **Optional runtime normalization (`normalize: bool = False`, `COMP-2`):** When `normalize=True` and the payload is a `dict`, applies the cached `PatchPlan` normalization rules in-memory (`apply_patch_plan_in_memory(raw_payload, plan)`) and passes the normalized `dict` to the wrapped handler; when `normalize=False` (default), passes the original payload unchanged.
    4. Invokes (and `await`s if async) the wrapped handler and returns its exact return value.

### 8.4 EXP-4: Deep Nested `BaseModel` & Multi-Element List Drift Synthesis (`src/sentinel/detector.py`, `src/synthesizer/patcher.py`)
- **Multi-Element List Inspection & Compatibility (`FEAS-1`, `FEAS-2`):**
  - Helper `_classify_list_kinds(items: list[Any], max_items: int = 256) -> str` inspects up to `max_items` elements: if empty, returns `str(PrimitiveKind.EMPTY_LIST)` (`"<empty_list>"`); otherwise collects `sorted({str(classify_scalar(el)) for el in items[:max_items]})` and returns `f"list[{'|'.join(kinds)}]"`.
  - In `detect_drift` (`src/sentinel/detector.py:465-483`), when Pydantic's `err["loc"]` contains an `int` list index (e.g. `('tags', 1)` where element `1` of a `list[str]` failed validation), `detect_drift` slices `field_loc` up to the first `int` index (`('tags',)`), looks up the parent list container via `_lookup_path(parsed, field_loc)`, classifies it via `_classify_list_kinds`, resolves `expected_type` at `field_loc` (`"list[str]"`), and deduplicates by `(kind, field_loc)` (`FEAS-1`).
  - In `_collect_extra_paths`, when an extra field value is a `list`, its observed kind is recorded via `_classify_list_kinds(val)` instead of `classify_scalar(val)` (`FEAS-2`).
  - In `_type_compatible`, when `"list"` is in `expected_type.lower()`, returns `True` if `observed_kind == PrimitiveKind.EMPTY_LIST` or `observed_kind.startswith("list[")` (`FEAS-2`).
- **Complete List Coercion & Deep Nested `BaseModel` Synthesis (`COMP-3`, `CLAR-3`, `FEAS-3`):**
  - `_coercion_lines_for_action` supports `list[str]`, `list[int]` (explicitly excluding `bool` via `and not isinstance(item, bool)`), `list[float]` (excluding `bool`), and `list[bool]` (`FEAS-3`).
  - Signature compatibility (`CLAR-3`): `build_patch_plan(report: DriftReport, root_model: type[BaseModel] | None = None) -> PatchPlan` and `synthesize_patch(source_code: str, report: DriftReport, root_model: type[BaseModel] | None = None) -> PatchResult` keep `root_model=None` as default so all existing callers continue to work unchanged. When `root_model` is provided (or inferred from CST class annotations in `synthesize_patch`) and `issue.path` has length `>= 2` crossing a child `BaseModel` field (e.g. `('billing', 'postal_code')` where `billing: BillingAddress`):
    - If `issue.candidate_source_path` is `None` or shares the child prefix `issue.path[:-1]` (e.g. `('billing', 'zip_code')`), `build_patch_plan` strips the prefix so `PatchAction(target_model="BillingAddress", target_field="postal_code", source_path=('zip_code',))` is relative to `BillingAddress` (`COMP-3`).
    - If `issue.candidate_source_path` lies outside the child prefix (e.g. root key `('billing_zip',)` moved into `('billing', 'postal_code')`), the action is attached to `root_model` (`target_model=report.model_name`) so the root validator copies `normalized['billing_zip']` into `normalized.setdefault('billing', {})['postal_code']` (`COMP-3`).
  - In `verify_patch`, after `exec(patch_result.patched_source, namespace)`, every `BaseModel` subclass in `namespace` calls `cls.model_rebuild(_types_namespace=namespace, force=True)` so child model validators are bound before `model_cls.model_validate` runs (`FEAS-3`).

### 8.5 EXP-5: TypeScript / Zod Cross-Language Adapter Emitter (`src/synthesizer/zod_emitter.py`)
- **Function Signature:** `synthesize_zod_preprocessor(plan: PatchPlan) -> str`
- **Behavior (`CLAR-4`):**
  - Groups `plan.actions` by `action.target_model` (preserving order, with `plan.model_name` emitted last) and generates a deterministic strict-TypeScript function `export function normalize<TargetModel>Drift(data: unknown): Record<string, unknown> | unknown` for each targeted model, plus `export function createSelfHealZodAdapter<T extends z.ZodTypeAny>(schema: T)` wrapping `z.preprocess(normalize<ModelName>Drift, schema)`.
  - Emits exact TypeScript equivalents for every `PatchAction`: key rename, nested object extraction, string/float-to-int (`Math.trunc(Number(val))`), string/int-to-float (`Number(val)`), string/int-to-boolean, scalar-to-array wrapping (`Array.isArray`), and null-to-default fallback.

### Step 0A–0B Reality Check (`gstack-plan-reality`)

| Premise | Claim | File:Line Receipt | Verdict |
|---|---|---|---|
| Zero-PII boundary redaction | `detect_drift` strips all scalar values into `PrimitiveKind` before building `DriftReport` | `src/sentinel/detector.py:67-105`, `tests/test_drift_and_patch.py:480-491` | pass |
| 100% drift detection recall & >=85% patch pass rate | 40/40 (100%) recall and 40/40 (100%) auto-patch verification across Stripe/HubSpot/Weather mutations | `tests/test_drift_and_patch.py:130-462`, `tests/test_drift_and_patch.py:517-552` | pass |
| Runtime check < 2ms | Median 0.017ms, p99 0.040ms across 7 runs of 40 mutations | `src/sentinel/detector.py:424`, `README.md:33-36` | pass |
| Strict sandbox verification | `verify_patch` validates CST syntax, drifted + canonical payloads (`strict=True`), and `mypy --strict` | `src/synthesizer/patcher.py:400-476`, `tests/test_drift_and_patch.py:555-572` | pass |
| End-to-end PR & multi-runtime delivery | `PROJECT_SPEC.md:9,15` claims TypeScript (Zod), Anthropic Claude fallback, and GitHub PR automation; EXP-1..EXP-5 add all 3 to this wave while Hosted GitHub App / Cloud Run server is deferred to `TODOS.md` (D8) | `PROJECT_SPEC.md:9-16`, `PROJECT_SPEC.md:59-135` | pass |

REALITY: premise-table pass 5 premises checked with receipts, unverified=0 PROJECT_SPEC.md:8-27
REALITY: already-done pass Core Python sentinel, libcst synthesizer, ADR-001, and 40-mutation suite are implemented in 3660b4e src/sentinel/detector.py:423
REALITY: surface-check pass Public Python exports and pytest/mypy/ruff commands verified against pyproject.toml and src/selfheal_api/__init__.py:1-38 pyproject.toml:1-39
REALITY: binding-decisions pass ADR-001 and AGENTS.md strict typing, zero-PII, and no bare except rules honored docs/adr/001-redacted-ast-synthesis-and-sandbox-gate.md:1-37
REALITY: numbers pass Recall 40/40 (100%) [measured: tests/test_drift_and_patch.py:546] and pass rate 40/40 (100%) [measured: tests/test_drift_and_patch.py:547] and latency median 0.017ms / p99 0.040ms [measured: tests/test_drift_and_patch.py:548-549] tests/test_drift_and_patch.py:520-550
REALITY: deferred-asks pass No linked GitHub issue; spec items tracked in TASKS.md TASKS.md:1-16
REALITY: pr-count pass Single-branch wave (pr_count=1 default) PROJECT_SPEC.md:23-27


## Completion Summary

```text
  +====================================================================+
  |            MEGA PLAN REVIEW — COMPLETION SUMMARY                   |
  +====================================================================+
  | Mode selected        | SCOPE EXPANSION                             |
  | System Audit         | 17-file Python core green (16/16 tests)     |
  | Step 0               | D1–D9 approved; EXP-1..EXP-5 accepted       |
  | Section 1  (Arch)    | 0 issues found                              |
  | Section 2  (Errors)  | 6 error paths mapped, 0 GAPS                |
  | Section 3  (Security)| 0 issues found, 0 High severity             |
  | Section 4  (Data/UX) | 4 shadow/async paths mapped, 0 unhandled    |
  | Section 5  (Quality) | 0 issues found                              |
  | Section 6  (Tests)   | 4 test suites planned, 0 gaps               |
  | Section 7  (Perf)    | 0 issues found (<0.05ms fast path)          |
  | Section 8  (Observ)  | 0 gaps found (structured selfheal_api log)  |
  | Section 9  (Deploy)  | 0 risks flagged (pure Python + git revert)  |
  | Section 10 (Future)  | Reversibility: 5/5, debt items: 1 (TODOS.md)|
  | Section 11 (Design)  | SKIPPED (no UI scope)                       |
  +--------------------------------------------------------------------+
  | NOT in scope         | written (1 deferred item: Cloud Run/GH App) |
  | What already exists  | written                                     |
  | Dream state delta    | written                                     |
  | Error/rescue registry| 6 rows, 0 CRITICAL GAPS                     |
  | Failure modes        | 5 total, 0 CRITICAL GAPS                    |
  | TODOS.md updates     | 1 item written (Hosted GH App & Cloud Run)  |
  | Scope proposals      | 5 proposed, 5 accepted (EXP-1..EXP-5)       |
  | CEO plan             | written (~/.gstack/projects/justinmafie/ceo-plans/2026-11-25-selfheal-api-expansion.md) |
  | Outside voice        | codex + unavailable (MODEL_UNUSABLE)        |
  | Lake Score           | 1/1 recommendations chose complete option   |
  | Diagrams produced    | 3 (Architecture, Data Flow, Temporal)       |
  | Stale diagrams found | 0                                           |
  | Unresolved decisions | 0 (none)                                    |
  | Reality rows         | 7/7 rows (PLAN_REALITY), 0 unverified       |
  +====================================================================+
```


## 10. Engineering Review (`plan-eng-review` — `FULL_REVIEW`)

- **Scope Challenge (`D1` / `plan-eng-review-structure`):** Selected `Original arrangement` (14 files, 4 new classes: `SchemaMappingProvider`, `AnthropicSchemaMappingProvider`, `DeterministicMappingProvider`, `DriftDeduplicator`). Scope Challenge result: `scope accepted as-is`.
- **Reality Check (`PLAN_REALITY`):** `phase=eng rows=7/7 missing=none unverified=0 verdict=complete`.

### Section 1: Architecture Review (Two Shapes Before One)
- **Shape 1 (Chosen — Modular Deep-Boundary Pipeline):** `sentinel` (`detector.py`, `llm_fallback.py`, `guard.py`) owns boundary parsing, zero-PII redaction, and pre-LLM deduplication; `synthesizer` (`patcher.py`, `zod_emitter.py`) owns `PatchPlan` IR and language emitters; `selfheal_api.cli` owns subprocess gates (`mypy --shadow-file`, `pytest`, `gh pr create`).
- **Shape 2 (Rejected — Monolithic `SelfHealEngine` Class):** Bundles HTTP I/O, threading LRU cache, and CST/Zod emission into one stateful class; rejected because it couples stateless `<0.05ms` detection with network/subprocess lifecycles and creates shallow pass-through methods.
- **Red Flags Screen:** Zero shallow modules, zero information leakage (`DriftReport` strips all scalars), single state owner (`DriftDeduplicator` under `threading.Lock`), zero hand-synced lists (`zod_emitter.py` derives directly from `PatchPlan.actions`).

### Section 2: Code Quality Review (`2` findings, `0` critical gaps)
- **[ENG-1] (Medium, Confidence 9/10, `PROJECT_SPEC.md:271` & `src/synthesizer/patcher.py`):** Share a single coercion/normalization helper between `synthesizer.patcher` and `apply_patch_plan_in_memory` (for `@selfheal_guard(..., normalize=True)`) rather than duplicating coercion rules across `guard.py` and `patcher.py`. Disposition: **Accepted** (incorporated into `T3`).
- **[ENG-2] (Low, Confidence 8/10, `PROJECT_SPEC.md:235`):** In `selfheal heal --open-pr`, stage and commit only the patched model file and optional `.ts` output via `git commit --only <files>` (and fail fast if those target files have pre-existing unstaged changes) so unrelated dirty workspace files are never swept into an automated PR. Disposition: **Accepted** (incorporated into `T5`).

### Section 3: Test Review & Coverage Diagram
Test Plan Artifact written to `/Users/justinmafie/.gstack/projects/justinmafie/justinmafie-main-eng-review-test-plan-20261125-183659.md`.

```text
CODE PATHS                                              USER FLOWS
[+] src/sentinel/detector.py (EXP-4)                    [+] CLI (selfheal check / heal)
  ├── [★★★ TESTED] detect_drift flat/40-mut — :517        ├── [GAP] check exit 0/1/2 — test_cli.py
  └── [GAP] list-index loc slice & union                  └── [GAP] heal gate abort + --open-pr — test_cli.py
[+] src/sentinel/llm_fallback.py (EXP-2)                [+] Webhook Guard (@selfheal_guard)
  ├── [GAP] [→EVAL] Anthropic HTTP + timeout/429          ├── [GAP] 1,000-burst pre-LLM dedup — test_guard.py
  └── [GAP] key regex sanitize & zero-PII check           └── [GAP] async to_thread + normalize — test_guard.py
[+] src/synthesizer/patcher.py + zod_emitter.py (EXP-4/5)
  ├── [★★★ TESTED] CST + mypy --strict — :555
  ├── [GAP] child BaseModel relative source_path + model_rebuild(force=True)
  └── [GAP] synthesize_zod_preprocessor per-model TS output

COVERAGE: 2/9 paths tested (22% baseline) | GAPS: 7 (planned in T1–T5)
```

### Section 4: Performance Review
- Fast-path `detect_drift` stays `<0.05ms` (`0.017ms` median measured); `_classify_list_kinds` caps at `max_items=256`; `DriftDeduplicator` caps memory at `max_fingerprints=1024`; `AnthropicSchemaMappingProvider` enforces `timeout=5.0` inside `asyncio.to_thread` for async handlers.

### Worktree Parallelization Strategy

| Step | Modules touched | Depends on |
|---|---|---|
| T1 (EXP-4) | `src/sentinel/detector.py`, `src/synthesizer/patcher.py`, `tests/test_nested_and_zod.py` | — |
| T2 (EXP-2) | `src/sentinel/llm_fallback.py`, `src/sentinel/detector.py`, `tests/test_llm_fallback.py` | T1 |
| T3 (EXP-3) | `src/sentinel/guard.py`, `src/sentinel/__init__.py`, `tests/test_guard.py` | T1 |
| T4 (EXP-5) | `src/synthesizer/zod_emitter.py`, `src/synthesizer/__init__.py`, `tests/test_nested_and_zod.py` | T1 |
| T5 (EXP-1) | `src/selfheal_api/cli.py`, `pyproject.toml`, `tests/test_cli.py` | T1, T2, T3, T4 |

- **Parallel lanes:** `Lane A: T1` → then `Lane B (T2)` + `Lane C (T3)` + `Lane D (T4)` in parallel → then `Lane E (T5)`.
- **Execution order:** Land `T1` first. Launch `T2`, `T3`, and `T4` in parallel worktrees (coordinate `detector.py` between `T1` and `T2`). Merge all three, then run `T5`.

### Eng Review Completion Summary
- Step 0: Scope Challenge — `scope accepted as-is` (`D1`: `Original arrangement`, 14 files, 4 classes)
- Reality rows: `7/7` applicable rows emitted (`PLAN_REALITY: phase=eng rows=7/7 verdict=complete`), `0` unverified premises
- Architecture Review: `0` issues found (`Two shapes before one` completed)
- Code Quality Review: `2` issues found (`ENG-1`, `ENG-2`, both accepted)
- Test Review: diagram produced, `7` planned coverage gaps mapped to `T1`–`T5`
- Performance Review: `0` issues found
- NOT in scope: written (Hosted GitHub App & GCP Cloud Run deferred to `TODOS.md`)
- What already exists: written (`sentinel/detector.py`, `synthesizer/patcher.py`, 16 passing tests)
- TODOS.md updates: `0` new items proposed in Eng Review (`1` carried from CEO Review)
- Failure modes: `0` critical gaps flagged
- Unresolved decisions: `0` in this review
- Outside voice: `codex` unavailable (`MODEL_UNUSABLE (gpt-6.2-astra)` and native `Plan`/`TaskStop` not offered)
- Parallelization: `5` lanes, `3` parallel (`T2`/`T3`/`T4` after `T1`) / `2` sequential (`T1`, `T5`)
- Lake Score: `N/A` (no coverage-scored questions)

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 3 | CLEAR | 5 proposals, 5 accepted, 1 deferred |
| Outside Review | `codex-plan-review` | Independent 2nd opinion | 6 | unavailable | MODEL_UNUSABLE (gpt-6.2-astra); no completed external review |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 4 | ISSUES OPEN | 2 issues, 0 critical gaps |
| Design Review | `/plan-design-review` | UI/UX gaps | 1 | STALE (>7d) | score: 5/10 → 8/10, 10 decisions (2026-10-13) |
| DX Review | `/plan-devex-review` | Developer experience gaps | 1 | STALE (>7d) | score: 6/10 → 9/10, TTHW: 15 min → < 2 min (2026-11-08) |

- **OUTSIDE COVERAGE:** codex (`plan-review`) unavailable — `MODEL_UNUSABLE (gpt-6.2-astra)` and native `Plan`/`TaskStop` not offered in Antigravity; no completed external review.
- **VERDICT:** CEO CLEARED — eng review mapped 2 resolved findings (`ENG-1`, `ENG-2`), 0 critical gaps, 0 unresolved decisions

NO UNRESOLVED DECISIONS
