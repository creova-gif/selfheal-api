# SelfHealAPI: Self-Maintaining APIs & Autonomous Schema-Drift Engine

SelfHealAPI detects third-party API payload drift at the system boundary, redacts all scalar values into structural primitives before analysis, and synthesizes `libcst` adapter patches that pass strict sandbox verification (`mypy --strict` and `pytest`) before a pull request is opened.

- **Spec and GTM doc:** [SelfHealAPI: Self-Maintaining APIs & Autonomous Schema-Drift Engine](https://docs.google.com/document/d/11etng86w_E0jCCEJ3lLjUW7L4yODKeIf4-yIe7J0EbY/edit?usp=drivesdk&ouid=110227624285917376281)
- **Portfolio strategy:** [Startup & Open-Source Cross-Project Strategy](https://docs.google.com/document/d/12Wpg5mMGnjnl2uhMBTOR0QqRzQj8kwfzTLpvLoRuB5Q/edit?usp=drivesdk&ouid=110227624285917376281)
- **Architecture and threat model:** See `AGENTS.md`, `docs/adr/001-redacted-ast-synthesis-and-sandbox-gate.md`, and `docs/THREAT_MODEL.md`.

## Architecture

1. **Sentinel (`src/sentinel/detector.py`)**: Validates incoming JSON payloads against Pydantic v2 models (`strict=True`), classifies every scalar into a zero-PII `PrimitiveKind` (`<str>`, `<int>`, `<int_str>`, `<float_str>`, `<bool_str>`, `<iso_datetime>`, `<null>`), and emits an immutable `DriftReport` with matched candidate source paths for renamed or nested fields.
2. **Synthesizer (`src/synthesizer/patcher.py`)**: Transforms a `DriftReport` into a `PatchPlan` and applies a `libcst.CSTTransformer` to inject a typed `@model_validator(mode='before')` normalization method and safe union/literal widening without losing formatting.
3. **Verification gate (`verify_patch`)**: Parses the patched CST, executes the model in an isolated namespace, validates the drifted payload and canonical legacy payload with `strict=True`, and verifies `mypy --strict` compliance.

## Quickstart

Run the linter, strict type checker, and test suite in Python 3.12:

```bash
.venv/bin/ruff check src tests
.venv/bin/mypy --strict src tests
.venv/bin/pytest -v
```

## `selfheal` CLI

Install the package (`pip install -e .`) to use the `selfheal` CLI:

```bash
# Detect schema drift and emit zero-PII DriftReport JSON (exit 0=clean, 1=drift, 2=error)
selfheal check --model path/to/models.py:Invoice --payload drifted.json

# Synthesize a libcst patch, run verify_patch + mypy --strict --shadow-file + pytest,
# preview the unified diff, emit a TypeScript Zod adapter, write the patch, and open a PR
selfheal heal \
  --model path/to/models.py:Invoice \
  --payload drifted.json \
  --canonical canonical.json \
  --diff \
  --zod generated/invoice_adapter.ts \
  --write \
  --open-pr
```

Useful `heal` flags:
- `--llm-fallback`: enable zero-PII `AnthropicSchemaMappingProvider` for opaque field renames.
- `--pytest-args "..."`: pass extra arguments to the `pytest` verification gate.
- `--branch <name>`: override the auto-generated `selfheal/<model>-<fingerprint>` branch name.

## Runtime Guard & Zero-PII LLM Fallback

Decorate sync or async boundary handlers with `@selfheal_guard` to detect drift in-process, deduplicate identical drift fingerprints, and optionally normalize incoming payloads (`normalize=True`):

```python
from sentinel.guard import DriftDeduplicator, selfheal_guard
from sentinel.llm_fallback import AnthropicSchemaMappingProvider

dedup = DriftDeduplicator(max_fingerprints=1024)
provider = AnthropicSchemaMappingProvider(max_calls=50)

@selfheal_guard(
    Invoice,
    mapping_provider=provider,
    deduplicator=dedup,
    normalize=True,
    on_drift=lambda report, fp: print(f"Drift detected ({fp[:8]}): {len(report.issues)} issues"),
)
async def handle_webhook(payload: dict[str, object]) -> Invoice:
    return Invoice.model_validate(payload)
```

- **`DriftDeduplicator`**: Thread-safe LRU cache with single-flight in-progress coordination keyed by `compute_drift_fingerprint(report)`.
- **`AnthropicSchemaMappingProvider(max_calls=...)`**: Sends only redacted `PrimitiveKind` schemas and field names (never raw scalar values), bounded by a per-instance `max_calls` budget.

## TypeScript / Zod Adapter Emission

Generate a typed TypeScript preprocessor alongside Python CST patches via `selfheal heal --zod <out.ts>` or programmatically with `synthesize_zod_preprocessor(plan)`:

```python
from synthesizer.zod_emitter import synthesize_zod_preprocessor

ts_source = synthesize_zod_preprocessor(patch_result.plan)
```

The emitted module exports `normalize<ModelName>Drift(data: unknown)` and `createSelfHealZodAdapter<T extends z.ZodTypeAny>(schema: T)` (wrapping `z.preprocess(...)`):

```ts
import { z } from 'zod';
import { createSelfHealZodAdapter } from './invoice_adapter';

const InvoiceSchema = createSelfHealZodAdapter(
  z.object({ id: z.string(), amount_cents: z.number().int() }),
);
```

## Hosted Multi-Tenant Webhook Server

Run the hosted webhook receiver locally or deploy to GCP Cloud Run using the repository `Dockerfile`:

```bash
python -m selfheal_api.server
```

Endpoints:
- `GET /healthz`: liveness/readiness health check.
- `POST /webhooks/drift`: multi-tenant drift ingestion and automated patch/PR dispatch endpoint.
- `GET /dashboard`: self-contained, XSS-safe HTML5 Drift Console displaying tenant quotas and recent redacted events.
- `GET /api/v1/events` & `GET /api/v1/summary`: SQLite WAL zero-PII drift event ledger and aggregate telemetry APIs.

## Milestone 4 & 5 Capabilities

- **Persistent Drift Ledger & Alerting:** `src/selfheal_api/ledger.py` records redacted drift metadata in SQLite WAL mode, while `src/selfheal_api/notifier.py` formats zero-PII Slack Block Kit and generic webhook alerts.
- **OpenAPI 3.1 `x-selfheal-aliases` Export:** Export OpenAPI 3.1.0 `components.schemas` JSON with `x-selfheal-aliases` and `x-selfheal-coercions` directly from the CLI:

```bash
selfheal heal --model app/models.py:Invoice --payload drifted.json --openapi openapi.json
```

- **Zero-Dependency ASGI/WSGI Middleware & Remediation Pipeline:** Drop-in `SelfHealASGIMiddleware` and `SelfHealWSGIMiddleware` inspect JSON payloads at runtime without framework lock-in, paired with a fail-closed `RemediationPipeline` that gates every synthesized patch through `verify_patch`, `mypy --strict`, and `pytest`.

## Reproducible benchmark numbers

Measured on Python 3.12.12 (`pydantic 2.14.4`, `libcst 1.9.0`) across the 40-mutation benchmark suite simulating Stripe, HubSpot, and Weather API drift (`tests/test_drift_and_patch.py`):

| Metric | Target threshold | Measured result | Evidence |
| --- | --- | --- | --- |
| Drift detection recall | 100% | **100.0%** (40 / 40) | `test_40_mutation_benchmark_thresholds` |
| Auto-patch verification pass rate | >= 85% | **100.0%** (40 / 40) | `test_40_mutation_benchmark_thresholds` |
| Sentinel runtime check latency (median) | < 2.0 ms | **0.017 ms** (range 0.016 to 0.020 ms across 7 runs) | Bound by `pydantic-core` validation + dict traversal on 1 core |
| Sentinel runtime check latency (p99) | < 2.0 ms | **0.040 ms** (range 0.033 to 0.059 ms across 7 runs) | Bound by `ValidationError` formatting and token scoring |

Regenerate the benchmark numbers with:

```bash
PYTHONPATH=src .venv/bin/python -c "
import statistics
from tests.test_drift_and_patch import ALL_40_MUTATIONS
from sentinel import detect_drift
from synthesizer import synthesize_patch, verify_patch

for _, m, _, _, mut in ALL_40_MUTATIONS:
    detect_drift(m, mut)

medians, p99s = [], []
for _ in range(7):
    s = sorted(detect_drift(m, mut).elapsed_ms for _, m, _, _, mut in ALL_40_MUTATIONS)
    medians.append(s[len(s) // 2])
    p99s.append(s[int(len(s) * 0.99)])

detected = sum(1 for _, m, _, _, mut in ALL_40_MUTATIONS if detect_drift(m, mut).has_drift)
passed = sum(1 for _, m, src, can, mut in ALL_40_MUTATIONS if verify_patch(synthesize_patch(src, detect_drift(m, mut)), mut, can).passed)
print(f'Recall: {detected}/{len(ALL_40_MUTATIONS)} | Pass: {passed}/{len(ALL_40_MUTATIONS)} | Median: {statistics.median(medians):.3f}ms | p99: {statistics.median(p99s):.3f}ms')
"
```
