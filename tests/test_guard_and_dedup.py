from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from sentinel import (
    DeterministicMappingProvider,
    DriftDeduplicator,
    compute_drift_fingerprint,
    detect_drift,
    selfheal_guard,
)


class _WebhookOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    amount: int


def test_dedup_reserve_and_begin_async_timeout() -> None:
    dedup = DriftDeduplicator(max_fingerprints=8)
    report = detect_drift(_WebhookOrder, {"x_ref_alpha": "a", "amount": "1"})
    fp = compute_drift_fingerprint(report)

    is_owner, res = dedup.reserve(fp)
    assert is_owner is True
    assert res is None

    # Second reserve is non-blocking and returns the in-flight entry
    is_owner2, entry = dedup.reserve(fp)
    assert is_owner2 is False
    assert entry is not None

    async def _wait_timeout() -> None:
        with pytest.raises(TimeoutError):
            await dedup.begin_async(fp, timeout=0.02)

    asyncio.run(_wait_timeout())
    dedup.abort(fp)

    # After abort, a new caller can acquire ownership
    is_owner3, res3 = dedup.reserve(fp)
    assert is_owner3 is True
    assert res3 is None
    dedup.abort(fp)


def test_async_guard_burst_with_single_worker_executor_no_deadlock() -> None:
    llm_calls = 0

    def resolver(
        _model_name: str,
        _missing_fields: dict[str, str],
        _extra_observed_kinds: dict[str, str],
    ) -> dict[str, tuple[str | int, ...]]:
        nonlocal llm_calls
        llm_calls += 1
        time.sleep(0.03)
        return {"id": ("x_ref_alpha",)}

    provider = DeterministicMappingProvider(resolver)
    dedup = DriftDeduplicator(max_fingerprints=16)

    @selfheal_guard(
        _WebhookOrder,
        mapping_provider=provider,
        deduplicator=dedup,
        normalize=True,
    )
    async def handle(payload: dict[str, Any]) -> _WebhookOrder:
        return _WebhookOrder.model_validate(payload, strict=True)

    async def _run() -> list[_WebhookOrder]:
        loop = asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            loop.set_default_executor(pool)
            tasks = [
                asyncio.create_task(
                    handle({"x_ref_alpha": f"ord_{i}", "amount": str(i + 1)})
                )
                for i in range(10)
            ]
            return await asyncio.wait_for(asyncio.gather(*tasks), timeout=5.0)

    results = asyncio.run(_run())
    assert llm_calls == 1
    assert [r.id for r in results] == [f"ord_{i}" for i in range(10)]
    assert [r.amount for r in results] == [i + 1 for i in range(10)]


def test_async_guard_cancel_owner_releases_reservation() -> None:
    provider_entered = threading.Event()
    release_provider = threading.Event()
    calls = 0

    def resolver(
        _model_name: str,
        _missing_fields: dict[str, str],
        _extra_observed_kinds: dict[str, str],
    ) -> dict[str, tuple[str | int, ...]]:
        nonlocal calls
        calls += 1
        if calls == 1:
            provider_entered.set()
            release_provider.wait(timeout=2.0)
        return {"id": ("x_ref_alpha",)}

    provider = DeterministicMappingProvider(resolver)
    dedup = DriftDeduplicator(max_fingerprints=16)

    @selfheal_guard(
        _WebhookOrder,
        mapping_provider=provider,
        deduplicator=dedup,
        normalize=True,
    )
    async def handle(payload: dict[str, Any]) -> _WebhookOrder:
        return _WebhookOrder.model_validate(payload, strict=True)

    async def _run() -> _WebhookOrder:
        owner_task = asyncio.create_task(
            handle({"x_ref_alpha": "ord_1", "amount": "10"})
        )
        while not provider_entered.is_set():
            await asyncio.sleep(0.005)

        owner_task.cancel()
        release_provider.set()
        with pytest.raises(asyncio.CancelledError):
            await owner_task

        return await asyncio.wait_for(
            handle({"x_ref_alpha": "ord_2", "amount": "20"}),
            timeout=2.0,
        )

    result = asyncio.run(_run())
    assert calls == 2
    assert result.id == "ord_2"
    assert result.amount == 20


def test_async_guard_cancel_duplicate_waiter_does_not_abort_owner() -> None:
    provider_entered = threading.Event()
    release_provider = threading.Event()
    calls = 0

    def resolver(
        _model_name: str,
        _missing_fields: dict[str, str],
        _extra_observed_kinds: dict[str, str],
    ) -> dict[str, tuple[str | int, ...]]:
        nonlocal calls
        calls += 1
        provider_entered.set()
        release_provider.wait(timeout=2.0)
        return {"id": ("x_ref_alpha",)}

    provider = DeterministicMappingProvider(resolver)
    dedup = DriftDeduplicator(max_fingerprints=16)

    @selfheal_guard(
        _WebhookOrder,
        mapping_provider=provider,
        deduplicator=dedup,
        normalize=True,
    )
    async def handle(payload: dict[str, Any]) -> _WebhookOrder:
        return _WebhookOrder.model_validate(payload, strict=True)

    async def _run() -> tuple[_WebhookOrder, _WebhookOrder]:
        owner_task = asyncio.create_task(
            handle({"x_ref_alpha": "ord_owner", "amount": "11"})
        )
        while not provider_entered.is_set():
            await asyncio.sleep(0.005)

        waiter_task = asyncio.create_task(
            handle({"x_ref_alpha": "ord_waiter", "amount": "12"})
        )
        await asyncio.sleep(0.01)
        assert not waiter_task.done()

        waiter_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter_task

        release_provider.set()
        owner_res = await asyncio.wait_for(owner_task, timeout=2.0)
        cached_res = await asyncio.wait_for(
            handle({"x_ref_alpha": "ord_cached", "amount": "13"}),
            timeout=2.0,
        )
        return owner_res, cached_res

    owner_res, cached_res = asyncio.run(_run())
    assert calls == 1
    assert owner_res.id == "ord_owner"
    assert owner_res.amount == 11
    assert cached_res.id == "ord_cached"
    assert cached_res.amount == 13
