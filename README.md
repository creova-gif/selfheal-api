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
