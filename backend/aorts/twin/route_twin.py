"""Route Twin: Track B's per-route model of capacity, primary load, recovery backlog, latency and
telemetry reliability.

Inputs and how each is protected:

* primary payment load       <- PAYMENT_DEBITED lifecycle events (recorded, deduplicated by event id);
* recovery backlog           <- count of unresolved Transaction Twins on the route;
* latency / workers / queue  <- INDEPENDENT switch-side measurements (snapshots, not events);
* telemetry reliability      <- verdicts of the TelemetryValidator (recorded ``telemetry_observations``),
                                as an EWMA: TRUSTED=1, SUSPICIOUS=0.3, REJECTED=0;
* reported telemetry values  <- kept for display only, never used to RAISE a capacity estimate.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

VERDICT_SCORE = {"TRUSTED": 1.0, "SUSPICIOUS": 0.3, "REJECTED": 0.0}


@dataclass
class RouteTwinState:
    route_id: str
    destination: str
    primary_events: deque = field(
        default_factory=lambda: deque(maxlen=5000)
    )  # timestamps of debits
    telemetry_reliability: float = 0.5  # unknown until observations arrive
    telemetry_observations: int = 0
    last_verdict: str | None = None
    last_reported: dict = field(default_factory=dict)
    measured: dict = field(default_factory=dict)
    measured_at: datetime | None = None
    measured_capacity_history: deque = field(default_factory=lambda: deque(maxlen=30))
    recovery_backlog: int = 0

    def primary_load_tps(self, now: datetime, window_s: float = 10.0) -> float:
        cutoff = now - timedelta(seconds=window_s)
        return round(sum(1 for t in self.primary_events if t >= cutoff) / window_s, 3)

    def as_dict(self, now: datetime) -> dict:
        return {
            "route_id": self.route_id,
            "destination": self.destination,
            "primary_load_tps": self.primary_load_tps(now),
            "recovery_backlog": self.recovery_backlog,
            "telemetry_reliability": round(self.telemetry_reliability, 3),
            "telemetry_observations": self.telemetry_observations,
            "last_verdict": self.last_verdict,
            "last_reported": self.last_reported,
            "measured": self.measured,
            "measured_at": self.measured_at.isoformat() if self.measured_at else None,
        }


class RouteTwinStore:
    def __init__(self, alpha: float = 0.3):
        self.routes: dict[str, RouteTwinState] = {}
        self.alpha = alpha

    def get(self, route_id: str) -> RouteTwinState:
        if route_id not in self.routes:
            self.routes[route_id] = RouteTwinState(route_id, route_id.split("-", 1)[1])
        return self.routes[route_id]

    def on_payment(self, route_id: str, at: datetime) -> None:
        self.get(route_id).primary_events.append(at)

    def on_telemetry(self, route_id: str, verdict: str, reported: dict) -> None:
        rt = self.get(route_id)
        score = VERDICT_SCORE.get(verdict, 0.0)
        rt.telemetry_reliability = (
            score
            if rt.telemetry_observations == 0
            else (1 - self.alpha) * rt.telemetry_reliability + self.alpha * score
        )
        rt.telemetry_observations += 1
        rt.last_verdict = verdict
        if verdict == "TRUSTED":
            rt.last_reported = {k: v for k, v in reported.items() if v is not None}

    def on_measurement(self, destination: str, measured: dict, at: datetime) -> None:
        for rt in [r for r in self.routes.values() if r.destination == destination]:
            rt.measured = dict(measured)
            rt.measured_at = at
            cap = measured_capacity(measured)
            if cap is not None:
                rt.measured_capacity_history.append(cap)


def measured_capacity(measured: dict | None) -> float | None:
    """Independent capacity estimate (requests/s) from switch-side measurements: workers / median latency."""
    if not measured or measured.get("workers") is None or not measured.get("latency_p50_ms"):
        return None
    return float(measured["workers"]) / max(float(measured["latency_p50_ms"]) / 1000.0, 0.001)
