"""Budget guard: Track B's last word on how many status checks a route may get per tick.

Whatever the controller recommends, the applied budget is

    min(recommended, hard cap, independent switch-side headroom)

and, when telemetry integrity for the route cannot be established (latest
observation SUSPICIOUS/REJECTED, no observation at all, or the latest TRUSTED observation is
older than ``max_telemetry_age_s``, i.e. the sensor went silent), the budget
is additionally limited to ``min(fallback budget, last budget applied while
telemetry was trusted)``. Suspicious telemetry can therefore never *raise* the
budget (freeze-or-reduce), and the system falls back to a documented safe policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class RouteBudget:
    route_id: str
    recommended: int
    applied: int
    reasons: list[str] = field(default_factory=list)
    integrity: str = "TRUSTED"

    def as_dict(self) -> dict:
        return {
            "route_id": self.route_id,
            "recommended": self.recommended,
            "applied": self.applied,
            "reasons": self.reasons,
            "integrity": self.integrity,
        }


def measured_headroom(measured: dict | None, tick_s: float, spare: float) -> int | None:
    """Checks per tick the route can absorb, from INDEPENDENT switch-side measurements.

    free workers = workers - in_flight - waiting; each check occupies a worker for about
    the measured median latency, so free workers can serve ``free * tick / p50`` checks per
    tick; only ``spare`` of that is offered to recovery traffic. Saturated route -> 1 (trickle).
    """
    if not measured or measured.get("workers") is None:
        return None
    free = (
        int(measured["workers"])
        - int(measured.get("in_flight") or 0)
        - int(measured.get("backlog") or 0)
    )
    if free <= 0:
        return 1
    p50 = max(float(measured.get("latency_p50_ms") or 0.0), 1.0)
    return max(1, int(free * (tick_s * 1000.0 / p50) * spare))


class BudgetGuard:
    def __init__(
        self,
        *,
        hard_cap: int,
        fallback_budget: int,
        enabled: bool = True,
        tick_s: float = 1.0,
        spare_fraction: float = 0.5,
        max_telemetry_age_s: float | None = None,
    ):
        self.hard_cap = hard_cap
        self.fallback = fallback_budget
        self.enabled = enabled
        self.tick_s = tick_s
        self.spare = spare_fraction
        self.max_age = max_telemetry_age_s
        self.last_trusted: dict[str, int] = {}

    def apply(
        self,
        route_id: str,
        recommended: int,
        *,
        telemetry: dict | None,
        measured: dict | None,
        enabled: bool | None = None,
        now: datetime | None = None,
    ) -> RouteBudget:
        enabled = self.enabled if enabled is None else enabled
        rb = RouteBudget(route_id, recommended, max(0, recommended))
        if rb.applied > self.hard_cap:
            rb.applied = self.hard_cap
            rb.reasons.append(f"hard cap {self.hard_cap}")
        if not enabled:
            rb.integrity = "UNCHECKED"
            rb.reasons.append("integrity guard disabled (experiment control arm)")
            return rb
        headroom = measured_headroom(measured, self.tick_s, self.spare)
        if headroom is not None and rb.applied > headroom:
            rb.applied = headroom
            rb.reasons.append(f"switch-measured headroom {headroom}/tick")
        verdict = (telemetry or {}).get("verdict")
        age = self._age_s(telemetry, now)
        if verdict == "TRUSTED" and age is not None and age > self.max_age:
            verdict = "STALE"  # a silent sensor is not evidence that the route is still healthy
            rb.reasons.append(f"latest trusted telemetry is {age:.0f}s old (> {self.max_age:g}s)")
        if verdict != "TRUSTED":
            rb.integrity = verdict or "NO_TELEMETRY"
            ceiling = min(self.fallback, self.last_trusted.get(route_id, self.fallback))
            if rb.applied > ceiling:
                rb.applied = ceiling
                rb.reasons.append(
                    f"telemetry integrity {rb.integrity}: conservative fallback budget {ceiling}"
                )
        else:
            self.last_trusted[route_id] = rb.applied
        return rb

    def _age_s(self, telemetry: dict | None, now: datetime | None) -> float | None:
        """Seconds since the observation behind ``telemetry`` (None when unknown or not checked)."""
        if self.max_age is None or not telemetry:
            return None
        ts = telemetry.get("observed_at") or telemetry.get("at")
        if not ts:
            return None
        when = datetime.fromisoformat(ts) if isinstance(ts, str) else ts
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return ((now or datetime.now(UTC)) - when).total_seconds()
