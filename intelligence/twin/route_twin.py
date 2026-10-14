"""Observable route projection; never a financial source of truth."""

from dataclasses import dataclass

from contracts.v1.models import RouteTelemetry


@dataclass
class RouteTwin:
    route_id: str
    last_sequence: int = -1
    latest: RouteTelemetry | None = None

    def observe(self, telemetry: RouteTelemetry) -> None:
        if telemetry.route_id != self.route_id:
            raise ValueError("telemetry belongs to another route")
        if telemetry.sequence <= self.last_sequence:
            raise ValueError("stale or replayed telemetry")
        self.last_sequence = telemetry.sequence
        self.latest = telemetry
