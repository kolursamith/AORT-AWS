"""Confidence-aware route capacity estimation (AORTS, Track B implementation).

Capacity is estimated from INDEPENDENT switch-side measurements (workers / median latency). Reported
telemetry values are not used as a capacity input at all; their validator verdicts only feed the
confidence (via reliability), which can only SHRINK the conservative bound. False high-capacity
telemetry therefore has no upward effect by construction. Confidence combines:

* measurement sample size     -> ``min(1, samples / full_samples)``;
* telemetry reliability       -> EWMA of validator verdicts (unknown = 0.5);
* measurement stability       -> 1 - coefficient of variation of recent capacity estimates (floor 0.5).

The conservative lower bound shrinks the point estimate as confidence drops:
``low = point * (1 - shrink * (1 - confidence))``.
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field

from backend.aorts.twin.route_twin import RouteTwinState, measured_capacity


@dataclass
class CapacityEstimate:
    route_id: str
    point_rps: float | None
    low_rps: float | None
    confidence: float
    sources: list[str] = field(default_factory=list)
    telemetry_reliability: float = 0.5

    def as_dict(self) -> dict:
        d = asdict(self)
        for k in ("point_rps", "low_rps", "confidence", "telemetry_reliability"):
            if isinstance(d[k], float):
                d[k] = round(d[k], 3)
        return d


class CapacityEstimator:
    def __init__(self, full_samples: int = 20, shrink: float = 0.8):
        self.full_samples = full_samples
        self.shrink = shrink

    def estimate(self, rt: RouteTwinState) -> CapacityEstimate:
        m = rt.measured or {}
        cap_m = measured_capacity(m)
        if cap_m is None:
            return CapacityEstimate(
                rt.route_id,
                None,
                None,
                0.0,
                ["no independent measurement"],
                rt.telemetry_reliability,
            )
        sources = [
            f"switch-measured {cap_m:.1f} rps (workers={m.get('workers')}, p50={m.get('latency_p50_ms')} ms)"
        ]
        point = cap_m
        # Reported telemetry is deliberately NOT a capacity input: it only affects confidence (via the
        # reliability of its verdicts), so a false high-capacity report can never raise the estimate.
        hist = list(rt.measured_capacity_history)
        cv = (
            (statistics.pstdev(hist) / statistics.fmean(hist))
            if len(hist) >= 3 and statistics.fmean(hist)
            else 0.5
        )
        stability = max(0.5, 1.0 - cv)
        sample_conf = min(1.0, (m.get("samples") or 0) / self.full_samples)
        reliability = rt.telemetry_reliability
        confidence = sample_conf * (0.5 + 0.5 * reliability) * stability
        low = point * (1 - self.shrink * (1 - confidence))
        sources.append(
            f"confidence {confidence:.2f} = samples {sample_conf:.2f} x reliability-factor "
            f"{0.5 + 0.5 * reliability:.2f} x stability {stability:.2f}"
        )
        return CapacityEstimate(rt.route_id, point, low, confidence, sources, reliability)
