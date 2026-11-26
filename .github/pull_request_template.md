## Summary
<!-- Briefly describe what this PR changes and why. -->

## Related Issues / ADRs
<!-- Link any related issues or docs/adr/NNN-*.md files. -->

## Type of Change
- [ ] Bug fix
- [ ] New feature
- [ ] Refactoring / performance improvement
- [ ] Documentation / repository polish

## Verification
Paste or summarize local verification output:
- `ruff check src tests`
- `mypy --strict src tests`
- `pytest -v`

## Checklist
- [ ] Code follows Python 3.12+, Pydantic v2, and `mypy --strict` standards.
- [ ] `ruff check src tests`, `mypy --strict src tests`, and `pytest -v` all pass locally.
- [ ] Deterministic unit tests added for all new behavior or bug fixes.
- [ ] Non-trivial design decisions recorded in `docs/adr/` using `docs/adr/000-template.md`.
- [ ] No `.env` files, API keys, secrets, or PII/PHI fixtures are committed.
- [ ] Documentation updated (`README.md`, `CHANGELOG.md`, docstrings) where applicable.
