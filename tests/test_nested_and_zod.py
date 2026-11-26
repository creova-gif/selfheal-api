from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from sentinel import DriftKind, detect_drift
from synthesizer import synthesize_patch, synthesize_zod_preprocessor, verify_patch


class BillingAddress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    city: str
    postal_code: str


class OrderEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: str
    billing: BillingAddress
    quantities: list[int]
    weights: list[float]
    flags: list[bool]


ORDER_SOURCE = """from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class BillingAddress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    city: str
    postal_code: str


class OrderEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: str
    billing: BillingAddress
    quantities: list[int]
    weights: list[float]
    flags: list[bool]
"""

ORDER_CANONICAL: dict[str, Any] = {
    "order_id": "ord_123",
    "billing": {"city": "San Francisco", "postal_code": "94107"},
    "quantities": [1, 2, 3],
    "weights": [1.5, 2.25],
    "flags": [True, False],
}


def test_heterogeneous_list_drift_slices_index_and_unions_kinds() -> None:
    mutated = {
        "order_id": "ord_123",
        "billing": {"city": "San Francisco", "postal_code": "94107"},
        "quantities": [1, "2", True],
        "weights": ["1.5", 2],
        "flags": ["true", 0],
    }
    report = detect_drift(OrderEvent, mutated)
    assert report.has_drift

    qty_issues = [i for i in report.issues if i.path == ("quantities",)]
    assert len(qty_issues) == 1
    assert qty_issues[0].observed_kind == "list[<bool>|<int>|<int_str>]"

    patch_res = synthesize_patch(ORDER_SOURCE, report, root_model=OrderEvent)
    assert patch_res.applied
    verification = verify_patch(patch_res, mutated, ORDER_CANONICAL)
    assert verification.passed, verification.error_detail

    ns: dict[str, Any] = {}
    exec(patch_res.patched_source, ns)
    rebuilt = ns["OrderEvent"]
    rebuilt.model_rebuild(_types_namespace=ns, force=True)
    parsed = rebuilt.model_validate(mutated, strict=True)
    # bool must be excluded from list[int] coercion (FEAS-3)
    assert parsed.quantities == [1, 2]
    assert parsed.weights == [1.5, 2.0]
    assert parsed.flags == [True, False]


def test_nested_basemodel_intra_child_rename_and_model_rebuild() -> None:
    mutated = {
        "order_id": "ord_123",
        "billing": {"city": "San Francisco", "zip_code": "94107"},
        "quantities": [1, 2],
        "weights": [1.5],
        "flags": [True],
    }
    report = detect_drift(OrderEvent, mutated)
    assert report.has_drift
    missing = [i for i in report.issues if i.kind == DriftKind.MISSING_FIELD]
    assert len(missing) == 1
    assert missing[0].path == ("billing", "postal_code")
    assert missing[0].candidate_source_path == ("billing", "zip_code")

    patch_res = synthesize_patch(ORDER_SOURCE, report, root_model=OrderEvent)
    assert any(
        a.target_model == "BillingAddress" and a.source_path == ("zip_code",)
        for a in patch_res.plan.actions
    )
    verification = verify_patch(patch_res, mutated, ORDER_CANONICAL)
    assert verification.passed, verification.error_detail


def test_nested_root_to_child_relocation_routes_to_root_model() -> None:
    mutated = {
        "order_id": "ord_123",
        "billing": {"city": "San Francisco"},
        "billing_postal_code": "94107",
        "quantities": [1],
        "weights": [1.5],
        "flags": [True],
    }
    report = detect_drift(OrderEvent, mutated)
    patch_res = synthesize_patch(ORDER_SOURCE, report, root_model=OrderEvent)
    assert any(
        a.target_model == "OrderEvent"
        and a.target_parent_path == ("billing",)
        and a.source_path == ("billing_postal_code",)
        for a in patch_res.plan.actions
    )
    verification = verify_patch(patch_res, mutated, ORDER_CANONICAL)
    assert verification.passed, verification.error_detail


def test_synthesize_zod_preprocessor_emits_per_model_normalizers() -> None:
    mutated = {
        "order_id": "ord_123",
        "billing": {"city": "San Francisco", "zip_code": "94107"},
        "quantities": [1, "2"],
        "weights": [1.5],
        "flags": ["true"],
    }
    report = detect_drift(OrderEvent, mutated)
    patch_res = synthesize_patch(ORDER_SOURCE, report, root_model=OrderEvent)
    zod_ts = synthesize_zod_preprocessor(patch_res.plan)

    assert "import { z } from 'zod';" in zod_ts
    assert "export function normalizeBillingAddressDrift" in zod_ts
    assert "export function normalizeOrderEventDrift" in zod_ts
    assert "export function createSelfHealZodAdapter" in zod_ts
    assert "z.preprocess(normalizeOrderEventDrift, schema)" in zod_ts
    assert "normalizeBillingAddressDrift(" in zod_ts


def test_list_of_nested_models_preserves_child_drift_path() -> None:
    class _LineItem(BaseModel):
        price: int

    class _Cart(BaseModel):
        items: list[_LineItem]

    drifted = {"items": [{"price": "12"}]}
    report = detect_drift(_Cart, drifted)
    assert report.has_drift
    assert any(i.path == ("items", 0, "price") for i in report.issues)


def test_list_of_nested_models_drift_at_nonzero_index() -> None:
    class _LineItem(BaseModel):
        price: int

    class _Cart(BaseModel):
        items: list[_LineItem]

    drifted = {"items": [{"price": 10}, {"price": "12"}]}
    report = detect_drift(_Cart, drifted)
    assert report.has_drift
    assert any(
        i.path == ("items", 0, "price") and i.observed_kind == "<int_str>"
        for i in report.issues
    )


def test_zod_preprocessor_handles_root_to_child_relocation() -> None:
    mutated = {
        "order_id": "ord_123",
        "billing": {"city": "San Francisco"},
        "billing_postal_code": "94107",
        "quantities": [1],
        "weights": [1.5],
        "flags": [True],
    }
    report = detect_drift(OrderEvent, mutated)
    patch_res = synthesize_patch(ORDER_SOURCE, report, root_model=OrderEvent)
    zod_ts = synthesize_zod_preprocessor(patch_res.plan)
    assert "normalized['billing']" in zod_ts
    assert "_parent_postal_code_0['postal_code'] = normalized['billing_postal_code']" in zod_ts
