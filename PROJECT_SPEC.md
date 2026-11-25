# SelfHealAPI: Self-Maintaining APIs & Autonomous Schema-Drift Engine

- **Model:** Open-Source MIT SDK + Hosted GitHub App ($49-$199/mo)
- **Thesis Alignment:** #10 Self-Maintaining APIs, #11 Cloud for Small Software
- **Full Google Doc Spec:** [SelfHealAPI: Self-Maintaining APIs & Autonomous Schema-Drift Engine](https://docs.google.com/document/d/11etng86w_E0jCCEJ3lLjUW7L4yODKeIf4-yIe7J0EbY/edit?usp=drivesdk&ouid=110227624285917376281)
- **Portfolio Strategy:** [00 - Startup & Open-Source Cross-Project Strategy](https://docs.google.com/document/d/12Wpg5mMGnjnl2uhMBTOR0QqRzQj8kwfzTLpvLoRuB5Q/edit?usp=drivesdk&ouid=110227624285917376281)

## 1. Goal & Problem
Detect third-party API payload drift in real time, synthesize PII-redacted AST adapter patches, and open test-verified GitHub PRs.

## 2. Target Customer (ICP) & Monetization
Backend/platform teams at SaaS startups maintaining 5-30 external REST/GraphQL/webhook integrations.

## 3. Tech Stack
Python 3.12 (Pydantic v2, libcst), TypeScript (Zod), Anthropic Claude, GitHub Actions/App, GCP Cloud Run.

## 4. Evaluation & Acceptance Thresholds
100% drift detection recall; >= 85% auto-patch PR pass rate across 40 injected API mutations; < 2ms runtime check.

## 5. Key Architectural Decision (ADR-001)
Require 100% passing sandbox tests + mypy --strict before opening a PR; redact all payload values before LLM synthesis.

## 6. Day 1–5 Quickstart
- [x] Set up sentinel/, synthesizer/, and tests/ directories
- [x] Implement detect_drift(expected_model, raw_json) with redacted payload snapshots
- [x] Build 10-mutation test suite simulating Stripe/HubSpot/Weather API drift
- [x] Verify libcst/Claude patch passes pytest and mypy cleanly
