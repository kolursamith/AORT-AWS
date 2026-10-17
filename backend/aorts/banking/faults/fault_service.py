"""Applies configured application-level faults (latency, errors, lost responses)."""

from __future__ import annotations

import hashlib
import random
import threading
import time
from dataclasses import dataclass

from backend.aorts.banking.faults.fault_config import RuntimeConfigStore
from backend.aorts.contracts.requests import FaultConfig


@dataclass
class FaultDecision:
    latency_ms: int = 0
    error: bool = False
    drop_response: bool = False
    unavailable: bool = False


class FaultInjector:
    """Seeded fault decisions.

    * ``stream`` - independent random stream per traffic class (e.g. ``credit`` vs ``query``), so the
      volume of status checks a recovery strategy sends cannot change which PAYMENTS hit a fault;
    * ``key``    - if given, the decision is a deterministic hash of (seed, stream, key, fault kind):
      the same payment gets the same fault outcome in every run with that seed, regardless of ordering.
      This is what makes strategy comparisons on "the same workload" actually equivalent.
    """

    def __init__(self, store: RuntimeConfigStore, seed: int = 42):
        self.store = store
        self.seed = seed
        self._rngs: dict[str, random.Random] = {}
        self._lock = threading.Lock()

    def _roll(self, p: float, stream: str, key: str | None, kind: str) -> bool:
        if p <= 0:
            return False
        if key is not None:
            digest = hashlib.sha256(f"{self.seed}|{stream}|{key}|{kind}".encode()).digest()
            return int.from_bytes(digest[:8], "big") / 2**64 < p
        with self._lock:
            rng = self._rngs.get(stream)
            if rng is None:
                rng = self._rngs[stream] = random.Random(f"{self.seed}|{stream}")
            return rng.random() < p

    def decide(
        self, *targets: str, stream: str = "default", key: str | None = None
    ) -> FaultDecision:
        """Combine faults for several targets (e.g. ``beneficiary:B`` and ``route:B``)."""
        out = FaultDecision()
        for target in targets:
            cfg: FaultConfig | None = self.store.fault(target)
            if cfg is None:
                continue
            out.latency_ms += cfg.latency_ms
            out.error = out.error or self._roll(cfg.error_rate, stream, key, f"{target}:error")
            out.drop_response = out.drop_response or self._roll(
                cfg.drop_response_rate, stream, key, f"{target}:drop"
            )
            out.unavailable = out.unavailable or cfg.unavailable
        return out

    def capacity_override(self, target: str) -> int | None:
        cfg = self.store.fault(target)
        return cfg.capacity_workers if cfg else None

    @staticmethod
    def sleep(decision: FaultDecision) -> None:
        if decision.latency_ms > 0:
            time.sleep(decision.latency_ms / 1000.0)
