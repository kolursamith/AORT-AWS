"""EWMA anomaly detector for route telemetry.

Keeps an exponentially weighted mean and variance per (route, metric), updated
ONLY with observations that passed validation (so an attacker cannot slowly drag
the baseline with rejected samples). A statistically plausible malicious sample
can still evade this detector; that is why it is one signal among several
(identity, schema, sequencing, independent switch measurements, conservative
fallback) and never sufficient proof of authenticity.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass


@dataclass
class _Stat:
    mean: float
    var: float
    n: int


class EwmaDetector:
    def __init__(
        self,
        alpha: float = 0.2,
        z_threshold: float = 4.0,
        warmup: int = 5,
        min_std_frac: float = 0.1,
    ):
        self.alpha = alpha
        self.z = z_threshold
        self.warmup = warmup
        self.min_std_frac = min_std_frac
        self._stats: dict[tuple[str, str], _Stat] = {}
        self._lock = threading.Lock()

    def score(self, route: str, metric: str, value: float) -> float | None:
        """Absolute z-score of ``value`` against the baseline, or None while warming up."""
        with self._lock:
            st = self._stats.get((route, metric))
            if st is None or st.n < self.warmup:
                return None
            std = max(math.sqrt(st.var), abs(st.mean) * self.min_std_frac, 1e-6)
            return abs(value - st.mean) / std

    def is_anomalous(self, route: str, metric: str, value: float) -> tuple[bool, float | None]:
        z = self.score(route, metric, value)
        return (z is not None and z > self.z), z

    def update(self, route: str, metric: str, value: float) -> None:
        with self._lock:
            st = self._stats.get((route, metric))
            if st is None:
                self._stats[(route, metric)] = _Stat(value, 0.0, 1)
                return
            diff = value - st.mean
            incr = self.alpha * diff
            st.mean += incr
            st.var = (1 - self.alpha) * (st.var + diff * incr)
            st.n += 1

    def baseline(self, route: str, metric: str) -> dict | None:
        with self._lock:
            st = self._stats.get((route, metric))
            return {"mean": st.mean, "std": math.sqrt(st.var), "n": st.n} if st else None
