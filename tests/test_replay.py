from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from selfheal_api.replay import (
    ReplayReport,
    ReplaySample,
    load_jsonl_samples,
    run_replay_corpus,
)
from sentinel.detector import detect_drift
from synthesizer.patcher import build_patch_plan


class _OrderRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: int
    amount: float
    currency: str


def test_load_jsonl_samples_skips_blank_lines(tmp_path: Path) -> None:
    jsonl_path = tmp_path / "samples.jsonl"
    lines = [
        "",
        json.dumps(
            {"sample_id": "s1", "payload": {"order_id": 1, "amount": 10.5, "currency": "USD"}}
        ),
        "   \n",
        json.dumps(
            {
                "sample_id": "s2",
                "payload": {"order_id": "bad", "amount": 10.5, "currency": "USD"},
                "expect_valid": False,
            }
        ),
    ]
    jsonl_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    samples = load_jsonl_samples(jsonl_path)
    assert len(samples) == 2
    assert samples[0] == ReplaySample(
        sample_id="s1",
        payload={"order_id": 1, "amount": 10.5, "currency": "USD"},
        expect_valid=True,
    )
    assert samples[1].sample_id == "s2"
    assert samples[1].expect_valid is False


def test_load_jsonl_samples_invalid_json_raises(tmp_path: Path) -> None:
    jsonl_path = tmp_path / "bad.jsonl"
    jsonl_path.write_text('{"sample_id": "s1", "payload": {}}\n{not valid json\n', encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid JSON on line 2"):
        load_jsonl_samples(jsonl_path)


def test_load_jsonl_samples_non_object_raises(tmp_path: Path) -> None:
    jsonl_path = tmp_path / "list.jsonl"
    jsonl_path.write_text("[1, 2, 3]\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Expected JSON object on line 1"):
        load_jsonl_samples(jsonl_path)


def test_load_jsonl_samples_exceeds_max_samples_raises(tmp_path: Path) -> None:
    jsonl_path = tmp_path / "many.jsonl"
    rows = [
        json.dumps({"sample_id": f"s{i}", "payload": {"order_id": i}})
        for i in range(4)
    ]
    jsonl_path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Exceeded max_samples limit of 3"):
        load_jsonl_samples(jsonl_path, max_samples=3)


def test_run_replay_corpus_clean() -> None:
    samples = [
        ReplaySample(
            sample_id="clean-1",
            payload={"order_id": 101, "amount": 25.0, "currency": "USD"},
        ),
        ReplaySample(
            sample_id="clean-2",
            payload={"order_id": 102, "amount": 99.95, "currency": "EUR"},
        ),
    ]

    report = run_replay_corpus(_OrderRecord, samples)
    assert report == ReplayReport(
        model_name="_OrderRecord",
        total=2,
        passed=2,
        failed=0,
        drifted=0,
        failures=(),
    )


def test_run_replay_corpus_drifted_without_and_with_patch_plan() -> None:
    drifted_payload = {
        "order_id": "42",
        "amount": "19.99",
        "currency": "USD",
        "secret_ssn": "123-45-6789",
    }
    samples = [ReplaySample(sample_id="drift-1", payload=drifted_payload)]

    without_plan = run_replay_corpus(_OrderRecord, samples)
    assert without_plan.model_name == "_OrderRecord"
    assert without_plan.total == 1
    assert without_plan.passed == 0
    assert without_plan.failed == 1
    assert without_plan.drifted == 1
    assert len(without_plan.failures) == 1
    failure_msg = without_plan.failures[0]
    assert failure_msg.startswith("drift-1: ValidationError at ")
    assert "order_id" in failure_msg
    assert "123-45-6789" not in failure_msg
    assert "19.99" not in failure_msg

    drift_report = detect_drift(_OrderRecord, drifted_payload)
    plan = build_patch_plan(drift_report, root_model=_OrderRecord)

    with_plan = run_replay_corpus(_OrderRecord, samples, plan=plan)
    assert with_plan == ReplayReport(
        model_name="_OrderRecord",
        total=1,
        passed=1,
        failed=0,
        drifted=1,
        failures=(),
    )


def test_run_replay_corpus_expect_valid_false() -> None:
    samples = [
        ReplaySample(
            sample_id="neg-pass",
            payload={"order_id": "not-an-int", "amount": 10.0, "currency": "USD"},
            expect_valid=False,
        ),
        ReplaySample(
            sample_id="neg-fail",
            payload={"order_id": 1, "amount": 10.0, "currency": "USD"},
            expect_valid=False,
        ),
    ]

    report = run_replay_corpus(_OrderRecord, samples)
    assert report.total == 2
    assert report.passed == 1
    assert report.failed == 1
    assert report.drifted == 1
    assert report.failures == ("neg-fail: ExpectedValidationFailure",)
