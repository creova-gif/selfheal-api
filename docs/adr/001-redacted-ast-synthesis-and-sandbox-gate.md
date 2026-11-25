# ADR-001: Redact Payload Values at Sentinel Boundary and Gate Auto-Patch PRs on Strict Sandbox Verification

- **Status:** Accepted
- **Owner:** Justin Mafie

## Context

Third-party SaaS APIs (such as Stripe, HubSpot, and Weather providers) introduce unannounced payload schema drift: field renames (`amount` to `amount_cents`), type coercions (`1999` to `"1999"`), nested path relocations (`customer_email` to `customer.email`), enum value expansions, and nullable field transitions. Traditional client integrations fail at runtime when `pydantic.BaseModel.model_validate` raises `ValidationError`.

Sending raw failing payloads to an LLM or external synthesizer to generate adapter patches exposes customer PII/PHI (emails, tokens, billing addresses, card metadata) across trust boundaries. In addition, applying unverified AST patches directly to a repository risks introducing syntax errors, type regressions, or broken runtime behavior.

We must meet three quantitative thresholds from the specification:
1. 100% drift detection recall across schema mutations.
2. Under 2ms p99 runtime check overhead on the sentinel path.
3. At least 85% auto-patch verification pass rate across a 40-mutation benchmark suite.

## Decision

1. **Structural Redaction Before Leaving the Sentinel Boundary:** `sentinel.detect_drift(expected_model, raw_json)` validates incoming payloads against the target Pydantic v2 model (`extra="forbid"` awareness or explicit extra-field inspection) and immediately transforms the raw payload into a zero-PII `RedactedSnapshot`. Every scalar value is replaced with a structural descriptor (`PrimitiveKind` such as `<str>`, `<int>`, `<int_str>`, `<float_str>`, `<bool_str>`, `<iso_datetime>`, `<null>`) while preserving exact JSON key paths and container hierarchy. Raw scalar values never enter `DriftReport` or reach the synthesizer.
2. **CST-Based Adapter Patch Synthesis (`libcst`):** Rather than mutating business logic or hand-editing raw strings, `synthesizer` parses the target model source module into a Concrete Syntax Tree (`libcst.Module`) and synthesizes a deterministic `@model_validator(mode="before")` normalization method (and safe widening for new optional/enum shapes where required) derived from `DriftIssue` classifications.
3. **Hard Verification Gate Before PR Creation:** Every synthesized CST patch must pass an isolated verification gate (`verify_patch`) before a pull request can be opened:
   - Syntactic and round-trip CST parse check.
   - Re-validation of the mutated payload against the patched model in an isolated namespace, plus regression validation that legacy canonical payloads still validate cleanly.
   - Static type check (`mypy --strict`) and test runner (`pytest`) execution when invoked in repository gate mode.

## Alternatives Considered

- **Send raw JSON payloads with regex PII scrubbing to an LLM:** Rejected. Regex scrubbing misses domain-specific secrets, customer names, and custom tokens. Structural type replacement guarantees zero scalar leakage by construction.
- **Runtime dynamic dict monkey-patching without code generation:** Rejected. Hides schema drift in memory without updating static types or leaving an auditable git commit and test trail.
- **Regex/string replacement on Python model files:** Rejected. Fragile around multi-line annotations, existing validators, and comments; `libcst` preserves formatting and guarantees valid Python syntax trees.

## Consequences & Failure Modes

- **Positive:** Zero PII/PHI leaves the sentinel boundary because only key names and structural value kinds (`PrimitiveKind`) are retained.
- **Positive:** `< 2ms` runtime detection latency (measured at `< 0.1ms` median in Python 3.12 with Pydantic v2 Rust core).
- **Negative:** Structural redaction discards semantic string content, so if an API renames a field to a completely unrelated opaque key (e.g., `customer_email` to `x_field_99`) with the same `<str>` type as another new field, heuristic key-similarity matching can be ambiguous when multiple fields of the same type change simultaneously.
- **Mitigation:** The synthesizer pairs structural type compatibility (`int` <-> `<int_str>`, `float` <-> `<float_str>`, `bool` <-> `<bool_str>`) with token/subsequence similarity and nested leaf-key matching, and the verification gate rejects any patch that fails strict validation.
