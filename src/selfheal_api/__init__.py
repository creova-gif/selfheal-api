from selfheal_api.middleware import (
    RouteBinding,
    SelfHealASGIMiddleware,
    SelfHealWSGIMiddleware,
)
from sentinel import (
    AnthropicSchemaMappingProvider,
    DeterministicMappingProvider,
    DriftDeduplicator,
    DriftIssue,
    DriftKind,
    DriftReport,
    PrimitiveKind,
    SchemaMappingProvider,
    compute_drift_fingerprint,
    detect_drift,
    redact_payload,
    selfheal_guard,
)
from synthesizer import (
    PatchAction,
    PatchPlan,
    PatchResult,
    VerificationResult,
    apply_patch_plan_in_memory,
    build_patch_plan,
    emit_openapi_document,
    emit_openapi_schema_patch,
    synthesize_patch,
    synthesize_zod_preprocessor,
    verify_patch,
)

__version__ = "0.2.0"


def healthcheck() -> str:
    return "ok"


__all__ = [
    "AnthropicSchemaMappingProvider",
    "DeterministicMappingProvider",
    "DriftDeduplicator",
    "DriftIssue",
    "DriftKind",
    "DriftReport",
    "PatchAction",
    "PatchPlan",
    "PatchResult",
    "PrimitiveKind",
    "RouteBinding",
    "SchemaMappingProvider",
    "SelfHealASGIMiddleware",
    "SelfHealWSGIMiddleware",
    "VerificationResult",
    "__version__",
    "apply_patch_plan_in_memory",
    "build_patch_plan",
    "compute_drift_fingerprint",
    "detect_drift",
    "emit_openapi_document",
    "emit_openapi_schema_patch",
    "healthcheck",
    "redact_payload",
    "selfheal_guard",
    "synthesize_patch",
    "synthesize_zod_preprocessor",
    "verify_patch",
]
