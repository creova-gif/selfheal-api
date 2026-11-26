from __future__ import annotations

import importlib
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


def test_cli_heal_openapi_writes_valid_openapi_31_with_aliases(tmp_path: Path) -> None:
    model_file = tmp_path / "models.py"
    model_file.write_text(MODEL_CODE, encoding="utf-8")
    drifted_json = tmp_path / "drifted.json"
    drifted_json.write_text(
        json.dumps({"id": "p1", "amount_cents": "250"}), encoding="utf-8"
    )
    openapi_out = tmp_path / "openapi.json"

    with (
        patch("selfheal_api.cli._run_mypy_shadow_gate", return_value=(True, "")),
        patch("selfheal_api.cli._run_pytest_gate", return_value=(True, "1 passed")),
    ):
        rc = main(
            [
                "heal",
                "--model",
                f"{model_file}:PaymentEvent",
                "--payload",
                str(drifted_json),
                "--openapi",
                str(openapi_out),
            ]
        )

    assert rc == 0
    assert openapi_out.exists()
    doc = json.loads(openapi_out.read_text(encoding="utf-8"))
    assert doc["openapi"] == "3.1.0"
    payment_schema = doc["components"]["schemas"]["PaymentEvent"]
    assert payment_schema["x-selfheal-aliases"] == {"amount": ["amount_cents"]}
    assert model_file.read_text(encoding="utf-8") == MODEL_CODE


def test_cli_heal_open_pr_rolls_back_openapi_on_failure_without_write(
    tmp_path: Path,
) -> None:
    model_file = tmp_path / "models.py"
    model_file.write_text(MODEL_CODE, encoding="utf-8")
    drifted_json = tmp_path / "drifted.json"
    drifted_json.write_text(
        json.dumps({"id": "p1", "amount_cents": 250}), encoding="utf-8"
    )
    openapi_out = tmp_path / "openapi.json"

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
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
                "--openapi",
                str(openapi_out),
                "--open-pr",
            ]
        )

    assert rc == 2
    assert model_file.read_text(encoding="utf-8") == MODEL_CODE
    assert not openapi_out.exists()


def test_cli_replay_clean_corpus_returns_0(tmp_path: Path, capsys: Any) -> None:
    model_file = tmp_path / "models.py"
    model_file.write_text(MODEL_CODE, encoding="utf-8")
    corpus = tmp_path / "clean.jsonl"
    corpus.write_text(
        "\n".join(
            [
                json.dumps({"sample_id": "s1", "payload": {"id": "p1", "amount": 100}}),
                json.dumps({"sample_id": "s2", "payload": {"id": "p2", "amount": 200}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    rc = main(["replay", "--model", f"{model_file}:PaymentEvent", "--corpus", str(corpus)])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report == {
        "model_name": "PaymentEvent",
        "total": 2,
        "passed": 2,
        "failed": 0,
        "drifted": 0,
        "failures": [],
    }


def test_cli_replay_drifted_corpus_fails_without_payload_and_passes_with_payload(
    tmp_path: Path, capsys: Any
) -> None:
    model_file = tmp_path / "models.py"
    model_file.write_text(MODEL_CODE, encoding="utf-8")
    corpus = tmp_path / "drifted.jsonl"
    corpus.write_text(
        "\n".join(
            [
                json.dumps({"sample_id": "s1", "payload": {"id": "p1", "amount": 100}}),
                json.dumps(
                    {"sample_id": "s2", "payload": {"id": "p2", "amount_cents": "250"}}
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    rc_fail = main(
        ["replay", "--model", f"{model_file}:PaymentEvent", "--corpus", str(corpus)]
    )
    assert rc_fail == 1
    fail_report = json.loads(capsys.readouterr().out)
    assert fail_report["total"] == 2
    assert fail_report["passed"] == 1
    assert fail_report["failed"] == 1
    assert fail_report["drifted"] == 1
    assert len(fail_report["failures"]) == 1

    drifted_json = tmp_path / "drifted.json"
    drifted_json.write_text(
        json.dumps({"id": "p2", "amount_cents": "250"}), encoding="utf-8"
    )
    rc_pass = main(
        [
            "replay",
            "--model",
            f"{model_file}:PaymentEvent",
            "--corpus",
            str(corpus),
            "--payload",
            str(drifted_json),
        ]
    )
    assert rc_pass == 0
    pass_report = json.loads(capsys.readouterr().out)
    assert pass_report["total"] == 2
    assert pass_report["passed"] == 2
    assert pass_report["failed"] == 0
    assert pass_report["failures"] == []


def test_cli_replay_missing_or_invalid_corpus_returns_2(tmp_path: Path) -> None:
    model_file = tmp_path / "models.py"
    model_file.write_text(MODEL_CODE, encoding="utf-8")

    missing_corpus = tmp_path / "missing.jsonl"
    rc_missing = main(
        [
            "replay",
            "--model",
            f"{model_file}:PaymentEvent",
            "--corpus",
            str(missing_corpus),
        ]
    )
    assert rc_missing == 2

    invalid_json_corpus = tmp_path / "invalid.jsonl"
    invalid_json_corpus.write_text("{not-json\n", encoding="utf-8")
    rc_invalid_json = main(
        [
            "replay",
            "--model",
            f"{model_file}:PaymentEvent",
            "--corpus",
            str(invalid_json_corpus),
        ]
    )
    assert rc_invalid_json == 2

    invalid_sample_corpus = tmp_path / "invalid_sample.jsonl"
    invalid_sample_corpus.write_text(json.dumps({"wrong": 1}) + "\n", encoding="utf-8")
    rc_invalid_sample = main(
        [
            "replay",
            "--model",
            f"{model_file}:PaymentEvent",
            "--corpus",
            str(invalid_sample_corpus),
        ]
    )
    assert rc_invalid_sample == 2

    valid_corpus = tmp_path / "valid.jsonl"
    valid_corpus.write_text(
        json.dumps({"sample_id": "s1", "payload": {"id": "p1", "amount": 100}}) + "\n",
        encoding="utf-8",
    )
    rc_missing_payload = main(
        [
            "replay",
            "--model",
            f"{model_file}:PaymentEvent",
            "--corpus",
            str(valid_corpus),
            "--payload",
            str(tmp_path / "missing_payload.json"),
        ]
    )
    assert rc_missing_payload == 2


def test_cli_doctor_json_and_never_leaks_api_key(monkeypatch: Any, capsys: Any) -> None:
    secret_key = "sk-ant-secret-do-not-leak-99999"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret_key)

    rc = main(["doctor", "--json"])
    assert rc == 0
    captured = capsys.readouterr()
    assert secret_key not in captured.out
    assert secret_key not in captured.err

    data = json.loads(captured.out)
    assert data["healthy"] is True
    assert data["python_version"]["ok"] is True
    assert data["packages"]["pydantic"]["available"] is True
    assert isinstance(data["packages"]["pydantic"]["version"], str)
    assert data["packages"]["libcst"]["available"] is True
    assert isinstance(data["packages"]["libcst"]["version"], str)
    assert set(data["binaries"].keys()) == {"ruff", "mypy", "pytest", "git", "gh"}
    assert data["anthropic_api_key_configured"] is True


def test_cli_doctor_human_readable_and_missing_dep_returns_1(
    monkeypatch: Any, capsys: Any
) -> None:
    secret_key = "sk-ant-secret-human-check-88888"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret_key)

    rc_human = main(["doctor"])
    assert rc_human == 0
    captured_human = capsys.readouterr()
    assert secret_key not in captured_human.out
    assert secret_key not in captured_human.err
    assert "python >= 3.12" in captured_human.out
    assert "ANTHROPIC_API_KEY (configured)" in captured_human.out

    real_import = importlib.import_module

    def fake_import(name: str, package: str | None = None) -> Any:
        if name == "libcst":
            raise ImportError("simulated missing libcst")
        return real_import(name, package)

    with patch("selfheal_api.cli.importlib.import_module", side_effect=fake_import):
        rc_fail = main(["doctor", "--json"])

    assert rc_fail == 1
    data = json.loads(capsys.readouterr().out)
    assert data["healthy"] is False
    assert data["packages"]["libcst"] == {"available": False, "version": None}
