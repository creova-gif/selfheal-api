from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, ConfigDict

from sentinel import (
    DeterministicMappingProvider,
    DriftDeduplicator,
    DriftReport,
    compute_drift_fingerprint,
    detect_drift,
    selfheal_guard,
)


class WebhookItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    amount: int


def test_fingerprint_is_deterministic_across_different_pii_values() -> None:
    r1 = detect_drift(WebhookItem, {"id": "a", "amount": "100"})
    r2 = detect_drift(WebhookItem, {"id": "secret-xyz", "amount": "99999"})
    assert compute_drift_fingerprint(r1) == compute_drift_fingerprint(r2)


def test_guard_deduplicates_1000_burst_before_llm_and_normalizes() -> None:
    llm_calls = 0
    drift_events: list[str] = []

    def resolver(
        model_name: str,
        missing_fields: dict[str, str],
        extra_observed_kinds: dict[str, str],
    ) -> dict[str, tuple[str | int, ...]]:
        nonlocal llm_calls
        llm_calls += 1
        return {"id": ("x_ref_alpha",)}

    def on_drift(report: DriftReport, fp: str) -> None:
        drift_events.append(fp)
        raise RuntimeError("telemetry sink down")

    dedup = DriftDeduplicator(max_fingerprints=16)
    provider = DeterministicMappingProvider(resolver)

    @selfheal_guard(
        WebhookItem,
        on_drift=on_drift,
        mapping_provider=provider,
        deduplicator=dedup,
        normalize=True,
    )
    def handle_webhook(payload: dict[str, Any]) -> WebhookItem:
        return WebhookItem.model_validate(payload, strict=True)

    for idx in range(1000):
        res = handle_webhook({"x_ref_alpha": f"id_{idx}", "amount": str(idx + 1)})
        assert res.id == f"id_{idx}"
        assert res.amount == idx + 1

    assert llm_calls == 1
    assert len(drift_events) == 1


def test_async_guard_runs_non_blocking_and_preserves_return() -> None:
    seen: list[str] = []

    @selfheal_guard(
        WebhookItem,
        on_drift=lambda _rep, fp: seen.append(fp),
        normalize=True,
    )
    async def async_handler(payload: dict[str, Any]) -> int:
        model = WebhookItem.model_validate(payload, strict=True)
        return model.amount

    out1 = asyncio.run(async_handler({"id": "1", "amount": "42"}))
    out2 = asyncio.run(async_handler({"id": "2", "amount": "99"}))
    assert out1 == 42
    assert out2 == 99
    assert len(seen) == 1


def test_guard_concurrent_first_drift_normalizes_all_callers() -> None:
    import concurrent.futures
    import time

    class _Item(BaseModel):
        item_id: int

    class _SlowProvider:
        def resolve_opaque_mappings(
            self,
            model_name: str,
            missing_fields: dict[str, str],
            extra_observed_kinds: dict[str, str],
        ) -> dict[str, tuple[str | int, ...]]:
            time.sleep(0.05)
            return {"item_id": ("id_legacy",)}

    @selfheal_guard(_Item, normalize=True, mapping_provider=_SlowProvider())
    def process(payload: dict[str, Any]) -> _Item:
        return _Item.model_validate(payload, strict=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futs = [pool.submit(process, {"id_legacy": 42}) for _ in range(4)]
        results = [f.result(timeout=5) for f in futs]

    assert all(r.item_id == 42 for r in results)


def test_guard_exception_does_not_poison_dedup() -> None:
    class _Item(BaseModel):
        item_id: int

    calls = 0

    class _FlakyProvider:
        def resolve_opaque_mappings(
            self,
            model_name: str,
            missing_fields: dict[str, str],
            extra_observed_kinds: dict[str, str],
        ) -> dict[str, tuple[str | int, ...]]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("transient outage")
            return {"item_id": ("xyz_opaque",)}

    @selfheal_guard(_Item, normalize=True, mapping_provider=_FlakyProvider())
    def process(payload: dict[str, Any]) -> _Item:
        return _Item.model_validate(payload, strict=True)

    import pytest

    with pytest.raises(RuntimeError, match="transient outage"):
        process({"xyz_opaque": 7})

    res = process({"xyz_opaque": 7})
    assert res.item_id == 7


def test_guard_nested_model_normalization_in_memory() -> None:
    class _Addr(BaseModel):
        postal_code: str

    class _Customer(BaseModel):
        name: str
        addr: _Addr

    @selfheal_guard(_Customer, normalize=True)
    def handle(payload: dict[str, Any]) -> _Customer:
        return _Customer.model_validate(payload, strict=True)

    out = handle({"name": "Ada", "addr": {"zip_code": "94107"}})
    assert out.addr.postal_code == "94107"
