# Contributing to SelfHealAPI

Thank you for your interest in contributing to **SelfHealAPI**! This guide covers local setup, code quality gates, architecture decision records, security/zero-PII rules, and our pull request checklist.

## Local Development Setup

SelfHealAPI requires **Python 3.12+**.

```bash
git clone https://github.com/creova-gif/selfheal-api.git
cd selfheal-api

python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Quality Gates

Every change must pass all three static-analysis and test gates before opening a pull request:

```bash
.venv/bin/ruff check src tests
.venv/bin/mypy --strict src tests
.venv/bin/pytest -v
```

Coding standards enforced across the codebase:
- **Python 3.12+**, **Pydantic v2**, and **`mypy --strict`** typing on all `src/` and `tests/` files.
- **Ruff** (`line-length = 100`, rules `E`, `F`, `I`, `UP`, `B`).
- Never use bare `except Exception: pass` — handle or log failures explicitly.
- Every new feature or bug fix must include deterministic unit tests.

## Architecture Decision Records (ADRs)

Record every non-trivial architectural or design decision in `docs/adr/` using [`docs/adr/000-template.md`](docs/adr/000-template.md). Number ADRs sequentially (e.g., `007-short-descriptive-title.md`) and include Context, Decision, Alternatives Considered, and Consequences & Failure Modes.

## Zero-PII & Security Standards

SelfHealAPI is built on a strict **Zero-PII boundary**:
- Never commit `.env` files, API keys, webhook secrets, or real customer/production payloads.
- Never commit PII or PHI fixtures in `tests/` or `docs/` — use synthetic, anonymized, or redacted `PrimitiveKind` examples only.
- Any LLM-facing or ledger-persisting code path must redact scalar values into structural primitives (`<str>`, `<int>`, `<float>`, `<bool>`, `<iso_datetime>`, `<null>`) before leaving the process or writing to disk.
- Review [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) when modifying sandbox execution, webhook verification, or CST patch synthesis.

## Pull Request Checklist

Before marking a pull request ready for review, verify:

- [ ] `ruff check src tests` passes with zero warnings or errors.
- [ ] `mypy --strict src tests` passes with zero errors.
- [ ] `pytest -v` passes with all tests green and no skipped regressions.
- [ ] Deterministic unit tests are included for all new behavior or bug fixes.
- [ ] Non-trivial design changes are documented in `docs/adr/` using `docs/adr/000-template.md`.
- [ ] No `.env` files, API keys, secrets, or PII/PHI fixtures are included in the diff.
- [ ] Documentation (`README.md`, `CHANGELOG.md`, docstrings) is updated if public APIs or CLI flags changed.
