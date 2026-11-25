# selfheal-api — Execution Checklist

## Wave 1: Day 1–5 Setup
- [x] Set up sentinel/, synthesizer/, and tests/ directories
- [x] Implement detect_drift(expected_model, raw_json) with redacted payload snapshots
- [x] Build 10-mutation test suite simulating Stripe/HubSpot/Weather API drift
- [x] Verify libcst/Claude patch passes pytest and mypy cleanly

## Wave 2: MVP & Eval Gate
- [x] Write ADR-001 in `docs/adr/`
- [x] Implement core pipeline + deterministic unit/eval tests
- [x] Verify CI workflow passes (`ruff`/`mypy` or `cargo clippy`/`test` or `go test -race`)

## Wave 3: Launch & Design Partner Validation
- [x] Publish clean README with reproducible benchmark numbers
- [ ] Run first external design-partner or open-source release
