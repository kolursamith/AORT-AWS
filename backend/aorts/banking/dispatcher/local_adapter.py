"""In-process dispatch queue with worker threads (no AWS needed)."""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable

from backend.aorts.contracts.events import DispatchJob

logger = logging.getLogger("aorts.dispatch.local")


class LocalQueue:
    def __init__(self, workers: int = 4, max_size: int = 10_000):
        self._q: queue.Queue[DispatchJob] = queue.Queue(maxsize=max_size)
        self.workers = workers
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()

    def send(self, job: DispatchJob) -> None:
        self._q.put_nowait(job)

    def depth(self) -> int:
        return self._q.qsize()

    def start(self, handler: Callable[[DispatchJob], None]) -> None:
        def _work():
            while not self._stop.is_set():
                try:
                    job = self._q.get(timeout=0.2)
                except queue.Empty:
                    continue
                try:
                    handler(job)
                except Exception:  # noqa: BLE001 - one bad job must not kill the worker
                    logger.exception("dispatch job failed: %s", job.job_id)
                finally:
                    self._q.task_done()

        for i in range(self.workers):
            t = threading.Thread(target=_work, name=f"dispatch-worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def drain(self, handler: Callable[[DispatchJob], None]) -> int:
        """Process everything queued synchronously (tests / deterministic experiments)."""
        n = 0
        while True:
            try:
                job = self._q.get_nowait()
            except queue.Empty:
                return n
            try:
                handler(job)
            finally:
                self._q.task_done()
            n += 1

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=2)


class DuplicatingQueue(LocalQueue):
    """Experiment knob: simulates at-least-once delivery by sending a (seeded) share of jobs twice.

    The duplicate carries the same ``expected_version``, so the ledger drops it as STALE_VERSION.
    """

    def __init__(self, prob: float, seed: int, workers: int = 8):
        super().__init__(workers=workers)
        import random

        self.prob, self.rng, self.duplicates = prob, random.Random(f"dup|{seed}"), 0

    def send(self, job: DispatchJob) -> None:
        super().send(job)
        if self.rng.random() < self.prob:
            self.duplicates += 1
            super().send(job)
