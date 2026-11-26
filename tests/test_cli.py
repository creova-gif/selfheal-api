from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

from selfheal_api.cli import main
from synthesizer.patcher import VerificationResult

MODEL_CODE = """from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class PaymentEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    amount: int
"""


def test_cli_check_exit_codes(tmp_path: Path, capsys: Any) -> None:
    model_file = tmp_path / "models.py"
    model_file.write_text(MODEL_CODE, encoding="utf-8")

    clean_json = tmp_path / "clean.json"
    clean_json.write_text(json.dumps({"id": "p1", "amount": 100}), encoding="utf-8")

    drifted_json = tmp_path / "drifted.json"
    drifted_json.write_text(
        json.dumps({"id": "p1", "amount_cents": "250"}), encoding="utf-8"
    )

    rc_clean = main(
        ["check", "--model", f"{model_file}:PaymentEvent", "--payload", str(clean_json)]
    )
    assert rc_clean == 0

    rc_drift = main(
        ["check", "--model", f"{model_file}:PaymentEvent", "--payload", str(drifted_json)]
    )
    assert rc_drift == 1
    out = capsys.readouterr().out
    assert '"has_drift": true' in out
    assert "250" not in out

    rc_bad = main(["check", "--model", "bad_spec", "--payload", str(clean_json)])
    assert rc_bad == 2


def test_cli_heal_diff_zod_and_write_gates_on_mypy_and_pytest(
    tmp_path: Path, capsys: Any
) -> None:
    model_file = tmp_path / "models.py"
    model_file.write_text(MODEL_CODE, encoding="utf-8")

    drifted_json = tmp_path / "drifted.json"
    drifted_json.write_text(
        json.dumps({"id": "p1", "amount_cents": "250"}), encoding="utf-8"
    )
    canonical_json = tmp_path / "canonical.json"
    canonical_json.write_text(json.dumps({"id": "p1", "amount": 100}), encoding="utf-8")
    zod_out = tmp_path / "payment.ts"

    # Gate failure aborts before touching model_file or zod_out
    with patch("selfheal_api.cli._run_mypy_shadow_gate", return_value=(False, "mypy error")):
        rc_fail = main(
            [
                "heal",
                "--model",
                f"{model_file}:PaymentEvent",
                "--payload",
                str(drifted_json),
                "--write",
                "--zod",
                str(zod_out),
            ]
        )
    assert rc_fail == 1
    assert model_file.read_text(encoding="utf-8") == MODEL_CODE
    assert not zod_out.exists()

    # Passing mypy + pytest gates writes diff, zod file, and patched model
    with (
        patch("selfheal_api.cli._run_mypy_shadow_gate", return_value=(True, "")),
        patch("selfheal_api.cli._run_pytest_gate", return_value=(True, "1 passed")),
    ):
        rc_ok = main(
            [
                "heal",
                "--model",
                f"{model_file}:PaymentEvent",
                "--payload",
                str(drifted_json),
                "--canonical",
                str(canonical_json),
                "--diff",
                "--write",
                "--zod",
                str(zod_out),
            ]
        )
    assert rc_ok == 0
    stdout = capsys.readouterr().out
    assert "--- a/models.py" in stdout
    assert "+++ b/models.py" in stdout
    assert zod_out.exists()
    assert "normalizePaymentEventDrift" in zod_out.read_text(encoding="utf-8")
    assert "_selfheal_normalize_drift" in model_file.read_text(encoding="utf-8")


def test_cli_heal_open_pr_invokes_git_commit_only_and_gh(tmp_path: Path) -> None:
    model_file = tmp_path / "models.py"
    model_file.write_text(MODEL_CODE, encoding="utf-8")
    drifted_json = tmp_path / "drifted.json"
    drifted_json.write_text(
        json.dumps({"id": "p1", "amount_cents": 250}), encoding="utf-8"
    )

    recorded_cmds: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        recorded_cmds.append(cmd)
        stdout = " M models.py\n" if cmd[:2] == ["git", "status"] else ""
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    with (
        patch("selfheal_api.cli._run_mypy_shadow_gate", return_value=(True, "")),
        patch("selfheal_api.cli._run_pytest_gate", return_value=(True, "1 passed")),
        patch("subprocess.run", side_effect=fake_run),
    ):
        rc = main(
            [
                "heal",
                "--model",
                f"{model_file}:PaymentEvent",
                "--payload",
                str(drifted_json),
                "--open-pr",
            ]
        )
    assert rc == 0
    assert any(cmd[:3] == ["git", "commit", "--only"] for cmd in recorded_cmds)
    assert any(cmd[:3] == ["gh", "pr", "create"] for cmd in recorded_cmds)


def test_cli_heal_pytest_gate_runs_against_patched_model_and_rolls_back(
    tmp_path: Path,
) -> None:
    model_file = tmp_path / "models.py"
    model_file.write_text(MODEL_CODE, encoding="utf-8")
    drifted_json = tmp_path / "drifted.json"
    drifted_json.write_text(
        json.dumps({"id": "p1", "amount_cents": 250}), encoding="utf-8"
    )

    observed_source_during_pytest: list[str] = []

    def fake_pytest_gate(_args: list[str]) -> tuple[bool, str]:
        observed_source_during_pytest.append(model_file.read_text(encoding="utf-8"))
        return False, "1 failed"

    with (
        patch("selfheal_api.cli._run_mypy_shadow_gate", return_value=(True, "")),
        patch("selfheal_api.cli._run_pytest_gate", side_effect=fake_pytest_gate),
    ):
        rc = main(
            [
                "heal",
                "--model",
                f"{model_file}:PaymentEvent",
                "--payload",
                str(drifted_json),
                "--write",
            ]
        )

    assert rc == 1
    assert observed_source_during_pytest
    assert "_selfheal_normalize_drift" in observed_source_during_pytest[0]
    assert model_file.read_text(encoding="utf-8") == MODEL_CODE


def test_cli_heal_canonical_missing_or_invalid_json_returns_2(tmp_path: Path) -> None:
    model_file = tmp_path / "models.py"
    model_file.write_text(MODEL_CODE, encoding="utf-8")
    drifted_json = tmp_path / "drifted.json"
    drifted_json.write_text(
        json.dumps({"id": "p1", "amount_cents": 250}), encoding="utf-8"
    )

    missing_canonical = tmp_path / "does_not_exist.json"
    rc_missing = main(
        [
            "heal",
            "--model",
            f"{model_file}:PaymentEvent",
            "--payload",
            str(drifted_json),
            "--canonical",
            str(missing_canonical),
        ]
    )
    assert rc_missing == 2

    invalid_canonical = tmp_path / "invalid.json"
    invalid_canonical.write_text("{not-json", encoding="utf-8")
    rc_invalid = main(
        [
            "heal",
            "--model",
            f"{model_file}:PaymentEvent",
            "--payload",
            str(drifted_json),
            "--canonical",
            str(invalid_canonical),
        ]
    )
    assert rc_invalid == 2


def test_cli_heal_verify_patch_failure_returns_1(tmp_path: Path) -> None:
    model_file = tmp_path / "models.py"
    model_file.write_text(MODEL_CODE, encoding="utf-8")
    drifted_json = tmp_path / "drifted.json"
    drifted_json.write_text(
        json.dumps({"id": "p1", "amount_cents": 250}), encoding="utf-8"
    )

    failed_ver = VerificationResult(
        passed=False,
        syntax_valid=True,
        drifted_payload_valid=False,
        legacy_payload_valid=True,
        error_detail="simulated verification failure",
    )
    with patch("selfheal_api.cli.verify_patch", return_value=failed_ver):
        rc = main(
            [
                "heal",
                "--model",
                f"{model_file}:PaymentEvent",
                "--payload",
                str(drifted_json),
            ]
        )
    assert rc == 1
    assert model_file.read_text(encoding="utf-8") == MODEL_CODE


def test_cli_heal_open_pr_rolls_back_on_gh_failure_without_write(
    tmp_path: Path,
) -> None:
    model_file = tmp_path / "models.py"
    model_file.write_text(MODEL_CODE, encoding="utf-8")
    drifted_json = tmp_path / "drifted.json"
    drifted_json.write_text(
        json.dumps({"id": "p1", "amount_cents": 250}), encoding="utf-8"
    )
    zod_out = tmp_path / "payment.ts"
    recorded_cmds: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        recorded_cmds.append(cmd)
        if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="main\n", stderr="")
        if cmd[:2] == ["git", "status"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=" M models.py\n", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            raise subprocess.CalledProcessError(1, cmd, stderr="gh auth failed")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with (
        patch("selfheal_api.cli._run_mypy_shadow_gate", return_value=(True, "")),
        patch("selfheal_api.cli._run_pytest_gate", return_value=(True, "1 passed")),
        patch("subprocess.run", side_effect=fake_run),
    ):
        rc = main(
            [
                "heal",
                "--model",
                f"{model_file}:PaymentEvent",
                "--payload",
                str(drifted_json),
                "--zod",
                str(zod_out),
                "--open-pr",
            ]
        )

    assert rc == 2
    assert model_file.read_text(encoding="utf-8") == MODEL_CODE
    assert not zod_out.exists()
    assert ["git", "checkout", "main"] in recorded_cmds
