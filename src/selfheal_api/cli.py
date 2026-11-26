from __future__ import annotations

import argparse
import difflib
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from selfheal_api.replay import load_jsonl_samples, run_replay_corpus
from sentinel.detector import detect_drift
from sentinel.guard import compute_drift_fingerprint
from sentinel.llm_fallback import AnthropicSchemaMappingProvider, SchemaMappingProvider
from synthesizer.openapi_emitter import emit_openapi_document, emit_openapi_schema_patch
from synthesizer.patcher import PatchPlan, build_patch_plan, synthesize_patch, verify_patch
from synthesizer.zod_emitter import synthesize_zod_preprocessor


def _load_model_from_spec(model_spec: str) -> tuple[Path, type[BaseModel]]:
    if ":" not in model_spec:
        raise ValueError(
            f"Invalid --model specification {model_spec!r}; expected <path/to/file.py:ClassName>"
        )
    file_part, class_name = model_spec.rsplit(":", 1)
    model_path = Path(file_part).resolve()
    if not model_path.is_file():
        raise FileNotFoundError(f"Model file not found: {model_path}")

    module_name = f"_selfheal_target_{model_path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, str(model_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load Python module spec from {model_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    model_cls = getattr(module, class_name, None)
    if not isinstance(model_cls, type) or not issubclass(model_cls, BaseModel):
        raise ValueError(
            f"Attribute {class_name!r} in {model_path} is not a Pydantic BaseModel subclass"
        )
    return model_path, model_cls


def _load_json_file(path_str: str) -> dict[str, Any]:
    p = Path(path_str)
    if not p.is_file():
        raise FileNotFoundError(f"JSON payload file not found: {p}")
    data: Any = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {p}, got {type(data).__name__}")
    return data


def _atomic_write(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_name, target)
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def _run_mypy_shadow_gate(model_path: Path, patched_source: str) -> tuple[bool, str]:
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", encoding="utf-8", delete=False
    ) as tmp:
        tmp.write(patched_source)
        tmp_path = Path(tmp.name)

    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "mypy",
                "--strict",
                "--shadow-file",
                str(model_path),
                str(tmp_path),
                str(model_path),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        ok = proc.returncode == 0
        detail = (proc.stdout + "\n" + proc.stderr).strip()
        return ok, detail
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, str(exc)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _run_pytest_gate(pytest_args: Sequence[str]) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", *pytest_args],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        ok = proc.returncode == 0
        detail = (proc.stdout + "\n" + proc.stderr).strip()
        return ok, detail
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, str(exc)


def _open_github_pr(
    model_path: Path,
    zod_path: Path | None,
    branch_name: str,
    model_name: str,
    fingerprint: str,
    openapi_path: Path | None = None,
) -> None:
    files_to_commit = [str(model_path)]
    if zod_path is not None:
        files_to_commit.append(str(zod_path))
    if openapi_path is not None:
        files_to_commit.append(str(openapi_path))

    status_proc = subprocess.run(
        ["git", "status", "--porcelain", "--", *files_to_commit],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if status_proc.returncode != 0:
        raise RuntimeError(f"git status failed: {status_proc.stderr.strip()}")
    if not status_proc.stdout.strip():
        raise RuntimeError("No modified or untracked files to commit for PR")

    subprocess.run(
        ["git", "checkout", "-B", branch_name],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    subprocess.run(
        ["git", "add", "--", *files_to_commit],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    commit_msg = f"fix(schema): self-heal {model_name} ({fingerprint[:8]})"
    subprocess.run(
        ["git", "commit", "--only", *files_to_commit, "-m", commit_msg],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    subprocess.run(
        ["git", "push", "-u", "origin", branch_name],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    pr_body = (
        f"Automated schema-drift adapter patch for `{model_name}`.\n\n"
        f"- Drift fingerprint: `{fingerprint}`\n"
        "- Verification gate: `verify_patch` + `mypy --strict --shadow-file` + `pytest` passed.\n"
    )
    subprocess.run(
        [
            "gh",
            "pr",
            "create",
            "--title",
            f"fix(schema): self-heal {model_name} drift ({fingerprint[:8]})",
            "--body",
            pr_body,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="selfheal",
        description="SelfHealAPI schema-drift detection, CST synthesis, and verification CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    check_p = sub.add_parser(
        "check", help="Detect schema drift and emit zero-PII DriftReport JSON"
    )
    check_p.add_argument(
        "--model", required=True, help="Target model as <path/to/file.py:ClassName>"
    )
    check_p.add_argument("--payload", required=True, help="Path to JSON payload file")
    check_p.add_argument(
        "--llm-fallback",
        action="store_true",
        help="Enable zero-PII AnthropicSchemaMappingProvider fallback for opaque renames",
    )

    heal_p = sub.add_parser(
        "heal", help="Synthesize, verify, and optionally write/PR schema-drift patch"
    )
    heal_p.add_argument(
        "--model", required=True, help="Target model as <path/to/file.py:ClassName>"
    )
    heal_p.add_argument(
        "--payload", required=True, help="Path to drifted JSON payload file"
    )
    heal_p.add_argument(
        "--canonical", default=None, help="Optional path to canonical legacy JSON"
    )
    heal_p.add_argument(
        "--llm-fallback", action="store_true", help="Enable zero-PII Anthropic fallback"
    )
    heal_p.add_argument(
        "--pytest-args", default="", help="Extra arguments passed to pytest gate"
    )
    heal_p.add_argument(
        "--diff", action="store_true", help="Print unified diff of patched model"
    )
    heal_p.add_argument(
        "--write", action="store_true", help="Write verified patch back to model file"
    )
    heal_p.add_argument(
        "--zod", default=None, help="Write TypeScript Zod preprocessor to output path"
    )
    heal_p.add_argument(
        "--openapi",
        default=None,
        help="Optional output path for OpenAPI 3.1 components.schemas JSON",
    )
    heal_p.add_argument(
        "--open-pr",
        action="store_true",
        help="Create branch, commit verified patch, and open GitHub PR",
    )
    heal_p.add_argument(
        "--branch", default=None, help="Target branch name for --open-pr"
    )

    replay_p = sub.add_parser(
        "replay",
        help="Replay a JSONL corpus of recorded payloads against a Pydantic model",
    )
    replay_p.add_argument(
        "--model", required=True, help="Target model as <path/to/file.py:ClassName>"
    )
    replay_p.add_argument(
        "--corpus",
        required=True,
        help="Path to JSONL corpus of ReplaySample records",
    )
    replay_p.add_argument(
        "--payload",
        default=None,
        help="Optional drifted payload JSON to synthesize a PatchPlan for replay",
    )
    return parser


def _cmd_replay(args: argparse.Namespace) -> int:
    try:
        _, model_cls = _load_model_from_spec(args.model)
        samples = load_jsonl_samples(args.corpus)
        plan: PatchPlan | None = None
        if args.payload is not None:
            drifted_payload = _load_json_file(args.payload)
            drift_report = detect_drift(model_cls, drifted_payload)
            plan = build_patch_plan(drift_report, root_model=model_cls)
    except (FileNotFoundError, ValueError, ImportError, json.JSONDecodeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    report = run_replay_corpus(model_cls, samples, plan=plan)
    print(report.model_dump_json(indent=2))
    return 0 if report.failed == 0 else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "replay":
        return _cmd_replay(args)

    try:
        model_path, model_cls = _load_model_from_spec(args.model)
        drifted_payload = _load_json_file(args.payload)
    except (FileNotFoundError, ValueError, ImportError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    provider: SchemaMappingProvider | None = (
        AnthropicSchemaMappingProvider() if args.llm_fallback else None
    )
    report = detect_drift(model_cls, drifted_payload, mapping_provider=provider)

    if args.command == "check":
        print(report.model_dump_json(indent=2))
        return 1 if report.has_drift else 0

    canonical_payload: dict[str, Any] | None = None
    if args.canonical is not None:
        try:
            canonical_payload = _load_json_file(args.canonical)
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    source_code = model_path.read_text(encoding="utf-8")
    patch_result = synthesize_patch(source_code, report, root_model=model_cls)

    verification = verify_patch(patch_result, drifted_payload, canonical_payload)
    if not verification.passed:
        print(f"verification gate failed: {verification.error_detail}", file=sys.stderr)
        return 1

    mypy_ok, mypy_detail = _run_mypy_shadow_gate(model_path, patch_result.patched_source)
    if not mypy_ok:
        print(f"mypy --strict gate failed:\n{mypy_detail}", file=sys.stderr)
        return 1

    pytest_args = shlex.split(args.pytest_args) if args.pytest_args else []
    keep_written = False
    _atomic_write(model_path, patch_result.patched_source)
    try:
        pytest_ok, pytest_detail = _run_pytest_gate(pytest_args)
        if not pytest_ok:
            print(f"pytest gate failed:\n{pytest_detail}", file=sys.stderr)
            return 1
        keep_written = bool(args.write or args.open_pr)
    finally:
        if not keep_written:
            _atomic_write(model_path, source_code)

    if args.diff:
        diff_lines = difflib.unified_diff(
            source_code.splitlines(keepends=True),
            patch_result.patched_source.splitlines(keepends=True),
            fromfile=f"a/{model_path.name}",
            tofile=f"b/{model_path.name}",
        )
        sys.stdout.write("".join(diff_lines))

    zod_path: Path | None = None
    zod_existed = False
    if args.zod is not None:
        zod_path = Path(args.zod)
        zod_existed = zod_path.exists()
        zod_code = synthesize_zod_preprocessor(patch_result.plan)
        _atomic_write(zod_path, zod_code)

    openapi_path: Path | None = None
    openapi_existed = False
    if args.openapi is not None:
        openapi_path = Path(args.openapi)
        openapi_existed = openapi_path.exists()
        spec = emit_openapi_schema_patch(model_cls, patch_result.plan)
        doc = emit_openapi_document([spec])
        _atomic_write(openapi_path, json.dumps(doc, indent=2) + "\n")

    if args.open_pr:
        fp = compute_drift_fingerprint(report)
        branch = args.branch or f"selfheal/{model_cls.__name__.lower()}-{fp[:8]}"
        orig_branch: str | None = None
        try:
            rev_proc = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            if rev_proc.returncode == 0:
                candidate = rev_proc.stdout.strip()
                if candidate and candidate != "HEAD":
                    orig_branch = candidate
        except (OSError, subprocess.TimeoutExpired):
            orig_branch = None

        try:
            _open_github_pr(
                model_path,
                zod_path,
                branch,
                model_cls.__name__,
                fp,
                openapi_path=openapi_path,
            )
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            OSError,
            RuntimeError,
        ) as exc:
            if not args.write:
                if orig_branch is not None:
                    try:
                        chk = subprocess.run(
                            ["git", "checkout", orig_branch],
                            check=False,
                            capture_output=True,
                            text=True,
                            timeout=15,
                        )
                        if chk.returncode != 0:
                            print(
                                f"warning: failed to restore branch {orig_branch}: "
                                f"{chk.stderr.strip()}",
                                file=sys.stderr,
                            )
                    except (OSError, subprocess.TimeoutExpired) as chk_exc:
                        print(
                            f"warning: failed to restore branch {orig_branch}: {chk_exc}",
                            file=sys.stderr,
                        )
                _atomic_write(model_path, source_code)
                if zod_path is not None and not zod_existed:
                    zod_path.unlink(missing_ok=True)
                if openapi_path is not None and not openapi_existed:
                    openapi_path.unlink(missing_ok=True)
            print(f"pr creation failed: {exc}", file=sys.stderr)
            return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
