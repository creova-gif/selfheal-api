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
