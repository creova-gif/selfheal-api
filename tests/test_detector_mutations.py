from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from sentinel import DriftKind, detect_drift
from synthesizer import apply_patch_plan_in_memory, synthesize_patch, verify_patch


class LineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku: str
    price: int


class CartPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cart_id: str
    items: list[LineItem]


CART_SOURCE = """from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class LineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku: str
    price: int


class CartPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cart_id: str
    items: list[LineItem]
"""

CART_CANONICAL: dict[str, Any] = {
    "cart_id": "cart_1",
    "items": [
        {"sku": "A1", "price": 100},
        {"sku": "B2", "price": 250},
    ],
}


def test_later_list_item_rename_heals_in_place() -> None:
    drifted = {
        "cart_id": "cart_1",
        "items": [
            {"sku": "A1", "price": 100},
            {"sku": "B2", "amount": 250},
        ],
    }
    report = detect_drift(CartPayload, drifted)
    assert report.has_drift

    missing = [i for i in report.issues if i.kind == DriftKind.MISSING_FIELD]
    assert len(missing) == 1
    assert missing[0].path == ("items", 0, "price")
    assert missing[0].candidate_source_path == ("items", 0, "amount")

    patch_res = synthesize_patch(CART_SOURCE, report, root_model=CartPayload)
    assert patch_res.applied
    assert any(
        a.target_model == "LineItem"
        and a.target_field == "price"
        and a.source_path == ("amount",)
        and a.nested_path == ("items",)
        and not a.target_parent_path
        for a in patch_res.plan.actions
    )

    verification = verify_patch(patch_res, drifted, CART_CANONICAL)
    assert verification.passed, verification.error_detail

    in_mem = apply_patch_plan_in_memory(drifted, patch_res.plan, root_model=CartPayload)
    assert isinstance(in_mem["items"], list)
    assert in_mem["items"][0]["price"] == 100
    assert in_mem["items"][1]["price"] == 250
