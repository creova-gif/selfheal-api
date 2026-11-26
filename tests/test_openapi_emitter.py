from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from sentinel.detector import DriftKind
from synthesizer.openapi_emitter import (
    OpenAPIPatchSpec,
    emit_openapi_document,
    emit_openapi_schema_patch,
)
from synthesizer.patcher import PatchAction, PatchPlan


class _Address(BaseModel):
    city: str
    zip_code: str


class _UserProfile(BaseModel):
    username: str
    age: int
    email: str
    is_active: bool
    address: _Address | None = None


def test_emit_openapi_schema_patch_renames_nested_and_coercions() -> None:
    plan = PatchPlan(
        model_name="_UserProfile",
        actions=(
            PatchAction(
                target_model="_UserProfile",
                target_field="username",
                drift_kind=DriftKind.MISSING_FIELD,
                expected_type="str",
                observed_kind="<str>",
                source_path=("user_name",),
            ),
            PatchAction(
                target_model="_UserProfile",
                target_field="email",
                drift_kind=DriftKind.MISSING_FIELD,
                expected_type="str",
                observed_kind="<str>",
                source_path=("contact", "primary", "email_addr"),
            ),
            PatchAction(
                target_model="_UserProfile",
                target_field="age",
                drift_kind=DriftKind.TYPE_MISMATCH,
                expected_type="int",
                observed_kind="<int_str>",
                source_path=None,
            ),
            PatchAction(
                target_model="_UserProfile",
                target_field="is_active",
                drift_kind=DriftKind.NULLABILITY_DRIFT,
                expected_type="bool",
                observed_kind="<null>",
                source_path=None,
            ),
        ),
    )

    spec = emit_openapi_schema_patch(_UserProfile, plan)

    assert isinstance(spec, OpenAPIPatchSpec)
    assert spec.model_name == "_UserProfile"
    assert spec.schema_version == "3.1.0"
    assert spec.aliases == {
        "username": ["user_name"],
        "email": ["contact.primary.email_addr"],
    }
    assert spec.coercions == {
        "age": "int",
        "is_active": "bool",
    }
    assert spec.json_schema["x-selfheal-aliases"] == spec.aliases
    assert spec.json_schema["x-selfheal-coercions"] == spec.coercions
    assert "properties" in spec.json_schema
    assert "$defs" in spec.json_schema

    assert spec.yaml_snippet.startswith("_UserProfile:\n")
    assert "  x-selfheal-aliases:\n" in spec.yaml_snippet
    assert "    username:\n      - user_name\n" in spec.yaml_snippet
    assert "    email:\n      - contact.primary.email_addr\n" in spec.yaml_snippet
    assert "  x-selfheal-coercions:\n" in spec.yaml_snippet
    assert "    age: int\n" in spec.yaml_snippet
    assert "    is_active: bool\n" in spec.yaml_snippet

    # Deterministic output on repeated calls
    spec_again = emit_openapi_schema_patch(_UserProfile, plan)
    assert spec_again.yaml_snippet == spec.yaml_snippet


def test_emit_openapi_schema_patch_deduplicates_aliases_and_handles_missing_without_source() -> (
    None
):
    plan = PatchPlan(
        model_name="_Address",
        actions=(
            PatchAction(
                target_model="_Address",
                target_field="zip_code",
                drift_kind=DriftKind.MISSING_FIELD,
                expected_type="str",
                observed_kind="<int_str>",
                source_path=("postal", "code"),
            ),
            PatchAction(
                target_model="_Address",
                target_field="zip_code",
                drift_kind=DriftKind.MISSING_FIELD,
                expected_type="str",
                observed_kind="<int_str>",
                source_path=("postal", "code"),
            ),
            PatchAction(
                target_model="_Address",
                target_field="city",
                drift_kind=DriftKind.MISSING_FIELD,
                expected_type="str",
                observed_kind="<missing>",
                source_path=None,
            ),
        ),
    )

    spec = emit_openapi_schema_patch(_Address, plan)

    assert spec.aliases == {"zip_code": ["postal.code"]}
    assert spec.coercions == {"zip_code": "str"}
    assert "city" not in spec.aliases
    assert "city" not in spec.coercions

    with pytest.raises(ValidationError):
        spec.model_name = "Other"


def test_emit_openapi_document_assembles_openapi_3_1() -> None:
    user_spec = emit_openapi_schema_patch(
        _UserProfile,
        PatchPlan(
            model_name="_UserProfile",
            actions=(
                PatchAction(
                    target_model="_UserProfile",
                    target_field="username",
                    drift_kind=DriftKind.MISSING_FIELD,
                    expected_type="str",
                    observed_kind="<str>",
                    source_path=("handle",),
                ),
            ),
        ),
    )
    addr_spec = emit_openapi_schema_patch(
        _Address,
        PatchPlan(model_name="_Address", actions=()),
    )

    doc = emit_openapi_document(
        [user_spec, addr_spec],
        title="Custom Heal API",
        version="1.2.3",
    )

    assert doc["openapi"] == "3.1.0"
    assert doc["info"] == {"title": "Custom Heal API", "version": "1.2.3"}
    schemas = doc["components"]["schemas"]
    assert set(schemas.keys()) == {"_UserProfile", "_Address"}
    assert schemas["_UserProfile"]["x-selfheal-aliases"] == {"username": ["handle"]}
    assert schemas["_Address"]["x-selfheal-aliases"] == {}
    assert schemas["_Address"]["x-selfheal-coercions"] == {}

    default_doc = emit_openapi_document([])
    assert default_doc == {
        "openapi": "3.1.0",
        "info": {"title": "SelfHealAPI Schemas", "version": "0.2.0"},
        "components": {"schemas": {}},
    }
