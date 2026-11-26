from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import inspect
import json
import logging
import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, ParamSpec, TypeVar

from pydantic import BaseModel

from sentinel.detector import DriftReport, detect_drift
from sentinel.llm_fallback import SchemaMappingProvider
from synthesizer.patcher import PatchPlan, apply_patch_plan_in_memory, build_patch_plan

_logger = logging.getLogger("selfheal_api")

P = ParamSpec("P")
R = TypeVar("R")


def compute_drift_fingerprint(report: DriftReport) -> str:
    items = sorted(
        (
            str(issue.kind),
            tuple(str(s) for s in issue.path),
            issue.expected_type,
            issue.observed_kind,
            tuple(str(s) for s in issue.candidate_source_path)
            if issue.candidate_source_path is not None
            else (),
        )
        for issue in report.issues
    )
    canonical = json.dumps(
        {"model": report.model_name, "issues": items},
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _resolve_future(fut: asyncio.Future[None]) -> None:
    if not fut.done():
        fut.set_result(None)


class _InFlightEntry:
    __slots__ = ("async_waiters", "event")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.async_waiters: list[
            tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]
        ] = []

    def notify_all(self) -> None:
        self.event.set()
        for loop, fut in self.async_waiters:
            if not loop.is_closed():
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(_resolve_future, fut)
        self.async_waiters.clear()


class DriftDeduplicator:
    def __init__(self, max_fingerprints: int = 1024) -> None:
        if max_fingerprints < 1:
            raise ValueError("max_fingerprints must be >= 1")
        self._max_fingerprints = max_fingerprints
        self._lock = threading.Lock()
        self._seen: OrderedDict[str, PatchPlan | None] = OrderedDict()
        self._in_flight: dict[str, _InFlightEntry] = {}

    def reserve(
        self, fingerprint: str
    ) -> tuple[bool, PatchPlan | _InFlightEntry | None]:
        """Non-blocking reservation: returns (True, None) if caller owns resolution,
        (False, plan) if already cached, or (False, entry) if another caller is in-flight.
        """
        with self._lock:
            if fingerprint in self._seen:
                self._seen.move_to_end(fingerprint)
                return False, self._seen[fingerprint]
            if fingerprint in self._in_flight:
                return False, self._in_flight[fingerprint]
            self._in_flight[fingerprint] = _InFlightEntry()
            return True, None

    def begin(self, fingerprint: str) -> tuple[bool, PatchPlan | None]:
        while True:
            is_owner, result = self.reserve(fingerprint)
            if is_owner:
                return True, None
            if isinstance(result, _InFlightEntry):
                result.event.wait()
                continue
            return False, result

    async def begin_async(
        self, fingerprint: str, *, timeout: float | None = None
    ) -> tuple[bool, PatchPlan | None]:
        """Async reservation that waits without occupying an executor thread."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout if timeout is not None else None
        while True:
            with self._lock:
                if fingerprint in self._seen:
                    self._seen.move_to_end(fingerprint)
                    return False, self._seen[fingerprint]
                if fingerprint not in self._in_flight:
                    self._in_flight[fingerprint] = _InFlightEntry()
                    return True, None
                entry = self._in_flight[fingerprint]
                fut = loop.create_future()
                entry.async_waiters.append((loop, fut))
            try:
                if deadline is not None:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise TimeoutError(
                            f"Timed out waiting for drift deduplication of {fingerprint[:8]}"
                        )
                    async with asyncio.timeout(remaining):
                        await fut
                else:
                    await fut
            finally:
                with self._lock:
                    with contextlib.suppress(ValueError):
                        entry.async_waiters.remove((loop, fut))

    def complete(self, fingerprint: str, plan: PatchPlan) -> None:
        with self._lock:
            self._seen[fingerprint] = plan
            self._seen.move_to_end(fingerprint)
            if len(self._seen) > self._max_fingerprints:
                self._seen.popitem(last=False)
            entry = self._in_flight.pop(fingerprint, None)
            if entry is not None:
                entry.notify_all()

    def abort(self, fingerprint: str) -> None:
        with self._lock:
            entry = self._in_flight.pop(fingerprint, None)
            if entry is not None:
                entry.notify_all()

    def check_and_record(self, fingerprint: str) -> bool:
        with self._lock:
            if fingerprint in self._seen:
                self._seen.move_to_end(fingerprint)
                return False
            self._seen[fingerprint] = None
            if len(self._seen) > self._max_fingerprints:
                self._seen.popitem(last=False)
            return True

    def get_cached_plan(self, fingerprint: str) -> PatchPlan | None:
        with self._lock:
            return self._seen.get(fingerprint)

    def set_cached_plan(self, fingerprint: str, plan: PatchPlan) -> None:
        self.complete(fingerprint, plan)


def _extract_payload_from_call(
    func: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]
) -> tuple[str | bytes | dict[str, Any] | None, str, int | None]:
    if "payload" in kwargs:
        val = kwargs["payload"]
        if isinstance(val, (str, bytes, dict)):
            return val, "kwarg", None
        return None, "none", None

    sig = inspect.signature(func)
    param_names = list(sig.parameters.keys())
    pos_idx = 0
    if param_names and param_names[0] in ("self", "cls"):
        pos_idx = 1
    if len(args) > pos_idx:
        val = args[pos_idx]
        if isinstance(val, (str, bytes, dict)):
            return val, "pos", pos_idx
    return None, "none", None


def _replace_payload_in_call(
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    mode: str,
    pos_idx: int | None,
    new_payload: str | bytes | dict[str, Any],
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    if mode == "kwarg":
        updated_kwargs = dict(kwargs)
        updated_kwargs["payload"] = new_payload
        return args, updated_kwargs
    if mode == "pos" and pos_idx is not None:
        args_list = list(args)
        args_list[pos_idx] = new_payload
        return tuple(args_list), kwargs
    return args, kwargs


def _safe_invoke_callback(
    on_drift: Callable[[DriftReport, str], None] | None,
    report: DriftReport,
    fingerprint: str,
) -> None:
    if on_drift is None:
        return
    try:
        on_drift(report, fingerprint)
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "on_drift callback raised for model=%s fp=%s: %s",
            report.model_name,
            fingerprint[:8],
            exc,
        )


def _normalize_raw_payload(
    raw_payload: str | bytes | dict[str, Any],
    model_cls: type[BaseModel],
    plan: PatchPlan,
) -> str | bytes | dict[str, Any]:
    if isinstance(raw_payload, dict):
        return apply_patch_plan_in_memory(raw_payload, plan, root_model=model_cls)
    try:
        decoded = (
            raw_payload.decode("utf-8")
            if isinstance(raw_payload, bytes)
            else raw_payload
        )
        parsed = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw_payload
    if not isinstance(parsed, dict):
        return raw_payload
    norm_dict = apply_patch_plan_in_memory(parsed, plan, root_model=model_cls)
    serialized = json.dumps(norm_dict)
    return serialized.encode("utf-8") if isinstance(raw_payload, bytes) else serialized


def selfheal_guard(
    model_cls: type[BaseModel],
    *,
    on_drift: Callable[[DriftReport, str], None] | None = None,
    mapping_provider: SchemaMappingProvider | None = None,
    deduplicator: DriftDeduplicator | None = None,
    normalize: bool = False,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    dedup = deduplicator or DriftDeduplicator()

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
                raw_payload, mode, pos_idx = _extract_payload_from_call(
                    func, args, kwargs
                )
                if raw_payload is not None:
                    report = detect_drift(model_cls, raw_payload, mapping_provider=None)
                    if report.has_drift:
                        pre_fp = compute_drift_fingerprint(report)
                        is_new, plan = await dedup.begin_async(pre_fp)
                        if is_new:
                            try:
                                enriched = (
                                    await asyncio.to_thread(
                                        detect_drift,
                                        model_cls,
                                        raw_payload,
                                        mapping_provider,
                                    )
                                    if mapping_provider is not None
                                    else report
                                )
                                plan = build_patch_plan(enriched, root_model=model_cls)
                                dedup.complete(pre_fp, plan)
                            except BaseException:
                                dedup.abort(pre_fp)
                                raise
                            _safe_invoke_callback(
                                on_drift, enriched, compute_drift_fingerprint(enriched)
                            )
                        if normalize and plan is not None:
                            norm_payload = _normalize_raw_payload(
                                raw_payload, model_cls, plan
                            )
                            args_out, kwargs_out = _replace_payload_in_call(
                                args, kwargs, mode, pos_idx, norm_payload
                            )
                            return await func(*args_out, **kwargs_out)
                return await func(*args, **kwargs)

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            raw_payload, mode, pos_idx = _extract_payload_from_call(
                func, args, kwargs
            )
            if raw_payload is not None:
                report = detect_drift(model_cls, raw_payload, mapping_provider=None)
                if report.has_drift:
                    pre_fp = compute_drift_fingerprint(report)
                    is_new, plan = dedup.begin(pre_fp)
                    if is_new:
                        try:
                            enriched = (
                                detect_drift(
                                    model_cls, raw_payload, mapping_provider=mapping_provider
                                )
                                if mapping_provider is not None
                                else report
                            )
                            plan = build_patch_plan(enriched, root_model=model_cls)
                            dedup.complete(pre_fp, plan)
                        except BaseException:
                            dedup.abort(pre_fp)
                            raise
                        _safe_invoke_callback(
                            on_drift, enriched, compute_drift_fingerprint(enriched)
                        )
                    if normalize and plan is not None:
                        norm_payload = _normalize_raw_payload(
                            raw_payload, model_cls, plan
                        )
                        args_out, kwargs_out = _replace_payload_in_call(
                            args, kwargs, mode, pos_idx, norm_payload
                        )
                        return func(*args_out, **kwargs_out)
            return func(*args, **kwargs)

        return sync_wrapper

    return decorator
