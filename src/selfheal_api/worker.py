from __future__ import annotations

import logging
import queue
import threading
from collections import OrderedDict
from typing import Any

from pydantic import BaseModel, ConfigDict

from selfheal_api.remediation import RemediationPipeline, RemediationRequest

logger = logging.getLogger(__name__)


class _StopSentinel:
    """Sentinel object used to signal the worker thread to exit."""


_STOP_SENTINEL = _StopSentinel()


class QueuedRemediationJob(BaseModel):
    """Immutable job payload enqueued for asynchronous drift remediation."""

    model_config = ConfigDict(frozen=True)

    request: RemediationRequest
    expected_model: type[BaseModel]
    drifted_payload: dict[str, Any]
    canonical_payload: dict[str, Any] | None = None
    fingerprint: str


class WorkerStats(BaseModel):
    """Thread-safe snapshot of remediation worker queue counters."""

    model_config = ConfigDict(frozen=True)

    enqueued: int
    processed: int
    succeeded: int
    failed: int
    dropped_full: int
    deduplicated: int


class RemediationQueue:
    """Bounded, deduplicating background worker queue for schema-drift remediation."""

    def __init__(
        self,
        pipeline: RemediationPipeline,
        *,
        max_queue_size: int = 256,
        max_seen_fingerprints: int = 4096,
    ) -> None:
        if max_queue_size < 1:
            raise ValueError("max_queue_size must be at least 1")
        if max_seen_fingerprints < 0:
            raise ValueError("max_seen_fingerprints must be non-negative")

        self._pipeline = pipeline
        self._max_queue_size = max_queue_size
        self._max_seen_fingerprints = max_seen_fingerprints

        self._queue: queue.Queue[QueuedRemediationJob | _StopSentinel] = queue.Queue(
            maxsize=max_queue_size
        )
        self._seen_fingerprints: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._in_flight: set[tuple[str, str]] = set()

        self._lock = threading.Lock()
        self._drained_cond = threading.Condition(self._lock)
        self._thread: threading.Thread | None = None
        self._stop_requested = False

        self._pending = 0
        self._enqueued = 0
        self._processed = 0
        self._succeeded = 0
        self._failed = 0
        self._dropped_full = 0
        self._deduplicated = 0

    def start(self) -> None:
        """Start the background daemon worker thread if not already running."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_requested = False
            self._thread = threading.Thread(
                target=self._run_loop,
                name="selfheal-remediation-worker",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the worker thread to stop and wait up to ``timeout`` seconds."""
        with self._lock:
            self._stop_requested = True
            thread = self._thread
        if thread is None or not thread.is_alive():
            with self._lock:
                self._thread = None
            return

        try:
            self._queue.put_nowait(_STOP_SENTINEL)
        except queue.Full:
            logger.debug(
                "RemediationQueue stop sentinel dropped because queue is full; "
                "worker will exit via stop flag"
            )

        thread.join(timeout=timeout)
        with self._lock:
            if not thread.is_alive():
                self._thread = None

    def enqueue(self, job: QueuedRemediationJob) -> bool:
        """Enqueue a remediation job non-blockingly with org+fingerprint deduplication."""
        dedup_id = (job.request.org_id, job.fingerprint)
        with self._lock:
            if self._max_seen_fingerprints > 0:
                if dedup_id in self._in_flight:
                    self._deduplicated += 1
                    return False
                if dedup_id in self._seen_fingerprints:
                    self._seen_fingerprints.move_to_end(dedup_id)
                    self._deduplicated += 1
                    return False

            try:
                self._queue.put_nowait(job)
            except queue.Full:
                self._dropped_full += 1
                return False

            if self._max_seen_fingerprints > 0:
                self._in_flight.add(dedup_id)

            self._enqueued += 1
            self._pending += 1
            return True

    def wait_until_drained(self, timeout: float = 5.0) -> bool:
        """Block until all enqueued jobs finish processing or ``timeout`` expires."""
        with self._drained_cond:
            return self._drained_cond.wait_for(lambda: self._pending == 0, timeout=timeout)

    def stats(self) -> WorkerStats:
        """Return a thread-safe snapshot of queue counters."""
        with self._lock:
            return WorkerStats(
                enqueued=self._enqueued,
                processed=self._processed,
                succeeded=self._succeeded,
                failed=self._failed,
                dropped_full=self._dropped_full,
                deduplicated=self._deduplicated,
            )

    def _run_loop(self) -> None:
        while True:
            with self._lock:
                if self._stop_requested:
                    break
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if isinstance(item, _StopSentinel):
                self._queue.task_done()
                break

            dedup_id = (item.request.org_id, item.fingerprint)
            succeeded = False
            try:
                outcome = self._pipeline.run(
                    req=item.request,
                    expected_model=item.expected_model,
                    drifted_payload=item.drifted_payload,
                    canonical_payload=item.canonical_payload,
                )
                succeeded = outcome.status in {"pr_opened", "no_drift"}
            except Exception:
                logger.exception(
                    "Remediation job failed for org=%s model=%s fingerprint=%s",
                    item.request.org_id,
                    item.request.model_name,
                    item.fingerprint,
                )
            finally:
                with self._drained_cond:
                    if self._max_seen_fingerprints > 0:
                        self._in_flight.discard(dedup_id)
                        if succeeded:
                            self._seen_fingerprints[dedup_id] = None
                            self._seen_fingerprints.move_to_end(dedup_id)
                            while len(self._seen_fingerprints) > self._max_seen_fingerprints:
                                self._seen_fingerprints.popitem(last=False)
                    self._processed += 1
                    if succeeded:
                        self._succeeded += 1
                    else:
                        self._failed += 1
                    self._pending -= 1
                    if self._pending == 0:
                        self._drained_cond.notify_all()
                self._queue.task_done()
