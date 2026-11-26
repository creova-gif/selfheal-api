from __future__ import annotations

import builtins
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from sentinel import DriftKind, detect_drift
from synthesizer import synthesize_patch, synthesize_zod_preprocessor, verify_patch
from synthesizer.patcher import (
    PatchAction,
    PatchPlan,
    apply_patch_plan_in_memory,
    build_patch_plan,
)


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


def test_zod_preprocessor_handles_nullability_drift_and_list_str() -> None:
    class _Notice(BaseModel):
        title: str
        count: int
        tags: list[str]

    drifted = {"title": None, "count": None, "tags": "urgent"}
    report = detect_drift(_Notice, drifted)
    plan = build_patch_plan(report, root_model=_Notice)
    zod_ts = synthesize_zod_preprocessor(plan)

    assert "if (normalized['title'] === null) {" in zod_ts
    assert "normalized['title'] = '';" in zod_ts
    assert "if (normalized['count'] === null) {" in zod_ts
    assert "normalized['count'] = 0;" in zod_ts
    assert "!Array.isArray(normalized['tags'])" in zod_ts
    assert ".map((v) => String(v))" in zod_ts


def test_zod_preprocessor_links_two_level_nested_models() -> None:
    class Meta(BaseModel):
        tag: str

    class Item(BaseModel):
        qty: int
        meta: Meta

    class Order(BaseModel):
        order_id: str
        items: list[Item]

    drifted = {
        "order_id": 1001,
        "items": [
            {"qty": "3", "meta": {"label": "gift"}},
        ],
    }
    report = detect_drift(Order, drifted)
    plan = build_patch_plan(report, root_model=Order)
    zod_ts = synthesize_zod_preprocessor(plan)

    assert "export function normalizeMetaDrift" in zod_ts
    assert "export function normalizeItemDrift" in zod_ts
    assert "export function normalizeOrderDrift" in zod_ts

    # Verify normalizeItemDrift links to normalizeMetaDrift on normalized['meta']
    item_section = zod_ts.split("export function normalizeItemDrift")[1].split(
        "export function normalizeOrderDrift"
    )[0]
    assert "normalized['meta']" in item_section
    assert "normalizeMetaDrift(" in item_section

    # Verify normalizeOrderDrift links to normalizeItemDrift on normalized['items']
    order_section = zod_ts.split("export function normalizeOrderDrift")[1]
    assert "normalized['items']" in order_section
    assert "normalizeItemDrift(" in order_section
    assert "z.preprocess(normalizeOrderDrift, schema)" in order_section


def test_apply_patch_plan_in_memory_zero_exec_and_coercions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _forbid_exec(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("exec() must not be called by apply_patch_plan_in_memory")

    monkeypatch.setattr(builtins, "exec", _forbid_exec)

    plan = PatchPlan(
        model_name="Order",
        actions=(
            PatchAction(
                target_model="Order",
                target_field="zip_code",
                drift_kind=DriftKind.MISSING_FIELD,
                expected_type="int",
                observed_kind="str",
                source_path=("meta", "legacy", "zip"),
                target_parent_path=("shipping", "address"),
            ),
            PatchAction(
                target_model="Order",
                target_field="order_id",
                drift_kind=DriftKind.MISSING_FIELD,
                expected_type="str",
                observed_kind="str",
                source_path=("legacyId",),
            ),
            PatchAction(
                target_model="Order",
                target_field="region",
                drift_kind=DriftKind.MISSING_FIELD,
                expected_type="str",
                observed_kind="str",
                source_path=("a", "b", "c", "region"),
            ),
            PatchAction(
                target_model="Order",
                target_field="missing_note",
                drift_kind=DriftKind.MISSING_FIELD,
                expected_type="str",
                observed_kind="missing",
                source_path=None,
            ),
            PatchAction(
                target_model="Order",
                target_field="retry_count",
                drift_kind=DriftKind.NULLABILITY_DRIFT,
                expected_type="int",
                observed_kind="none",
            ),
            PatchAction(
                target_model="Order",
                target_field="optional_note",
                drift_kind=DriftKind.NULLABILITY_DRIFT,
                expected_type="str | None",
                observed_kind="none",
            ),
            PatchAction(
                target_model="Order",
                target_field="amount",
                drift_kind=DriftKind.TYPE_MISMATCH,
                expected_type="int",
                observed_kind="str",
            ),
            PatchAction(
                target_model="Order",
                target_field="flag_not_int",
                drift_kind=DriftKind.TYPE_MISMATCH,
                expected_type="int",
                observed_kind="bool",
            ),
            PatchAction(
                target_model="Order",
                target_field="score",
                drift_kind=DriftKind.TYPE_MISMATCH,
                expected_type="float",
                observed_kind="str",
            ),
            PatchAction(
                target_model="Order",
                target_field="active",
                drift_kind=DriftKind.TYPE_MISMATCH,
                expected_type="bool",
                observed_kind="str",
            ),
            PatchAction(
                target_model="Order",
                target_field="label_val",
                drift_kind=DriftKind.TYPE_MISMATCH,
                expected_type="str",
                observed_kind="dict",
            ),
            PatchAction(
                target_model="Order",
                target_field="label_name",
                drift_kind=DriftKind.TYPE_MISMATCH,
                expected_type="str",
                observed_kind="dict",
            ),
            PatchAction(
                target_model="Order",
                target_field="label_id",
                drift_kind=DriftKind.TYPE_MISMATCH,
                expected_type="str",
                observed_kind="dict",
            ),
            PatchAction(
                target_model="Order",
                target_field="tags",
                drift_kind=DriftKind.TYPE_MISMATCH,
                expected_type="list[str]",
                observed_kind="str",
            ),
            PatchAction(
                target_model="Order",
                target_field="counts",
                drift_kind=DriftKind.TYPE_MISMATCH,
                expected_type="list[int]",
                observed_kind="list",
            ),
            PatchAction(
                target_model="Order",
                target_field="ratios",
                drift_kind=DriftKind.TYPE_MISMATCH,
                expected_type="list[float]",
                observed_kind="list",
            ),
            PatchAction(
                target_model="Order",
                target_field="toggles",
                drift_kind=DriftKind.TYPE_MISMATCH,
                expected_type="list[bool]",
                observed_kind="list",
            ),
        ),
    )

    payload: dict[str, Any] = {
        "meta": {"legacy": {"zip": "94107"}},
        "legacyId": "ord-99",
        "a": {"b": {"c": {"region": "us-west"}}},
        "retry_count": None,
        "optional_note": None,
        "amount": "42",
        "flag_not_int": True,
        "score": "3.5",
        "active": "yes",
        "label_val": {"value": "v1"},
        "label_name": {"name": "n1"},
        "label_id": {"id": "i1"},
        "tags": "priority",
        "counts": ["1", 2.0, True],
        "ratios": ["1.5", 2, False],
        "toggles": ["true", "0", 1],
    }

    normalized = apply_patch_plan_in_memory(payload, plan)

    assert normalized["shipping"]["address"]["zip_code"] == 94107
    assert normalized["order_id"] == "ord-99"
    assert normalized["region"] == "us-west"
    assert normalized["missing_note"] == ""
    assert normalized["retry_count"] == 0
    assert normalized["optional_note"] is None
    assert normalized["amount"] == 42
    assert normalized["flag_not_int"] is True
    assert normalized["score"] == 3.5
    assert normalized["active"] is True
    assert normalized["label_val"] == "v1"
    assert normalized["label_name"] == "n1"
    assert normalized["label_id"] == "i1"
    assert normalized["tags"] == ["priority"]
    assert normalized["counts"] == [1, 2]
    assert normalized["ratios"] == [1.5, 2.0]
    assert normalized["toggles"] == [True, False, True]
