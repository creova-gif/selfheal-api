from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from sentinel.detector import detect_drift
from synthesizer.patcher import PatchPlan, apply_patch_plan_in_memory


class ReplaySample(BaseModel):
    """Single recorded payload sample for regression replay."""

    model_config = ConfigDict(frozen=True)

    sample_id: str
    payload: dict[str, Any]
    expect_valid: bool = True


class ReplayReport(BaseModel):
    """Summary of replaying a corpus against a Pydantic model."""

    model_config = ConfigDict(frozen=True)

    model_name: str
    total: int
    passed: int
    failed: int
    drifted: int
    failures: tuple[str, ...] = ()


def load_jsonl_samples(
    path: str | Path,
    *,
    max_samples: int = 5000,
) -> list[ReplaySample]:
    """Load ReplaySample records from a JSONL file, skipping blank lines."""
    if max_samples < 0:
        raise ValueError("max_samples must be non-negative")

    file_path = Path(path)
    samples: list[ReplaySample] = []
    with file_path.open("r", encoding="utf-8") as handle:
        for line_no, raw_line in enumerate(handle, start=1):
            stripped = raw_line.strip()
            if not stripped:
                continue
            if len(samples) >= max_samples:
                raise ValueError(f"Exceeded max_samples limit of {max_samples}")
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on line {line_no}") from exc
            if not isinstance(parsed, dict):
                raise ValueError(f"Expected JSON object on line {line_no}")
            try:
                sample = ReplaySample.model_validate(parsed)
            except ValidationError as exc:
                raise ValueError(
                    f"Invalid ReplaySample on line {line_no}: {type(exc).__name__}"
                ) from exc
            samples.append(sample)
    return samples


def _format_failure(sample_id: str, exc: Exception) -> str:
    """Format a failure string using exception type and error locs without raw PII."""
    if isinstance(exc, ValidationError):
        locs = ", ".join(
            ".".join(str(part) for part in err.get("loc", ())) or "<root>"
            for err in exc.errors()
        )
        return f"{sample_id}: {type(exc).__name__} at {locs}"
    return f"{sample_id}: {type(exc).__name__}"


def run_replay_corpus(
    model_cls: type[BaseModel],
    samples: Sequence[ReplaySample],
    *,
    plan: PatchPlan | None = None,
) -> ReplayReport:
    """Replay samples against model_cls, optionally applying a PatchPlan first."""
    passed = 0
    failed = 0
    drifted = 0
    failures: list[str] = []

    for sample in samples:
        drift_report = detect_drift(model_cls, sample.payload)
        if drift_report.has_drift:
            drifted += 1

        try:
            candidate = (
                apply_patch_plan_in_memory(sample.payload, plan, root_model=model_cls)
                if plan is not None
                else sample.payload
            )
            model_cls.model_validate(candidate, strict=True)
        except Exception as exc:  # noqa: BLE001
            if not sample.expect_valid:
                passed += 1
            else:
                failed += 1
                failures.append(_format_failure(sample.sample_id, exc))
        else:
            if sample.expect_valid:
                passed += 1
            else:
                failed += 1
                failures.append(f"{sample.sample_id}: ExpectedValidationFailure")

    return ReplayReport(
        model_name=model_cls.__name__,
        total=len(samples),
        passed=passed,
        failed=failed,
        drifted=drifted,
        failures=tuple(failures),
    )
