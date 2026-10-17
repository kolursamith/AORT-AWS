"""In-process metrics: labelled counters, latency reservoirs and a per-second time series.

When ``AORTS_EMF_ENABLED=true`` a CloudWatch Embedded Metric Format line is also
printed once per second; the awslogs driver on ECS turns those into CloudWatch
metrics without any PutMetricData API calls.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections import defaultdict, deque


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


class Metrics:
    def __init__(
        self, service: str, window_seconds: int = 600, emf: bool = False, namespace: str = "AORTS"
    ):
        self.service = service
        self.emf = emf
        self.namespace = namespace
        self._lock = threading.Lock()
        self.counters: dict[str, float] = defaultdict(float)
        self._lat: dict[str, deque] = defaultdict(lambda: deque(maxlen=5000))  # (ts, ms)
        self._series: deque = deque(maxlen=window_seconds)
        self._bucket_second = int(time.time())
        self._bucket: dict[str, float] = defaultdict(float)
        self.gauges: dict[str, float] = {}

    @staticmethod
    def key(name: str, **labels) -> str:
        if not labels:
            return name
        return name + "{" + ",".join(f"{k}={labels[k]}" for k in sorted(labels)) + "}"

    def _roll(self, now: float) -> None:
        sec = int(now)
        if sec != self._bucket_second:
            snapshot = {
                "t": self._bucket_second,
                **self._bucket,
                **{f"gauge:{k}": v for k, v in self.gauges.items()},
            }
            self._series.append(snapshot)
            if self.emf and self._bucket:
                self._emit_emf(snapshot)
            self._bucket = defaultdict(float)
            self._bucket_second = sec

    def inc(self, name: str, value: float = 1.0, **labels) -> None:
        k = self.key(name, **labels)
        now = time.time()
        with self._lock:
            self._roll(now)
            self.counters[k] += value
            self._bucket[k] += value

    def observe_latency(self, name: str, ms: float, **labels) -> None:
        k = self.key(name, **labels)
        with self._lock:
            self._lat[k].append((time.time(), ms))

    def set_gauge(self, name: str, value: float, **labels) -> None:
        with self._lock:
            self.gauges[self.key(name, **labels)] = value

    def latency_stats(self, name: str, window_s: float = 30.0, **labels) -> dict:
        k = self.key(name, **labels)
        cutoff = time.time() - window_s
        with self._lock:
            vals = [ms for ts, ms in self._lat.get(k, ()) if ts >= cutoff]
        return {
            "count": len(vals),
            "p50_ms": percentile(vals, 0.5),
            "p95_ms": percentile(vals, 0.95),
            "mean_ms": (sum(vals) / len(vals)) if vals else None,
        }

    def rate(self, name: str, window_s: int = 10, **labels) -> float:
        """Average events/second over the last ``window_s`` completed seconds."""
        k = self.key(name, **labels)
        with self._lock:
            self._roll(time.time())
            recent = list(self._series)[-window_s:]
        if not recent:
            return 0.0
        return sum(s.get(k, 0.0) for s in recent) / window_s

    def series(self, last_seconds: int = 120) -> list[dict]:
        with self._lock:
            self._roll(time.time())
            return list(self._series)[-last_seconds:]

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "service": self.service,
                "counters": dict(self.counters),
                "gauges": dict(self.gauges),
            }

    def reset(self) -> None:
        with self._lock:
            self.counters.clear()
            self._lat.clear()
            self._series.clear()
            self._bucket = defaultdict(float)

    def _emit_emf(self, snapshot: dict) -> None:
        metrics = {
            k.replace("{", "_").replace("}", "").replace(",", "_").replace("=", "-"): v
            for k, v in snapshot.items()
            if k != "t" and not k.startswith("gauge:")
        }
        doc = {
            "_aws": {
                "Timestamp": snapshot["t"] * 1000,
                "CloudWatchMetrics": [
                    {
                        "Namespace": self.namespace,
                        "Dimensions": [["Service"]],
                        "Metrics": [{"Name": n, "Unit": "Count"} for n in metrics],
                    }
                ],
            },
            "Service": self.service,
            **metrics,
        }
        sys.stdout.write(json.dumps(doc) + "\n")
