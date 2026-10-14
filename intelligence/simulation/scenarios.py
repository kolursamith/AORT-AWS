"""Small deterministic scenarios used by the no-backend simulator."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Scenario:
    name: str
    duration_seconds: int
    payment_rps: float
    route_capacity_rps: float
    fault_at_second: int | None


HACKATHON_SCENARIO = Scenario(
    name="switch-slowdown",
    duration_seconds=30,
    payment_rps=80.0,
    route_capacity_rps=100.0,
    fault_at_second=10,
)
