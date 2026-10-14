"""Validate telemetry identity, sequence, time, and conservative anomaly bounds."""

from datetime import UTC, datetime, timedelta

from contracts.v1.models import RouteTelemetry


class TelemetryValidator:
    def __init__(
        self, allowed_sources: set[str], max_clock_skew: timedelta = timedelta(seconds=30)
    ) -> None:
        self.allowed_sources = frozenset(allowed_sources)
        self.max_clock_skew = max_clock_skew
        self._last_sequence: dict[tuple[str, str], int] = {}

    def validate(self, item: RouteTelemetry, *, now: datetime | None = None) -> float:
        current = now or datetime.now(UTC)
        if item.source_id not in self.allowed_sources:
            raise ValueError("untrusted telemetry source")
        if abs(current - item.timestamp) > self.max_clock_skew:
            raise ValueError("telemetry timestamp outside permitted skew")
        key = (item.source_id, item.route_id)
        if item.sequence <= self._last_sequence.get(key, -1):
            raise ValueError("stale or replayed telemetry sequence")
        self._last_sequence[key] = item.sequence
        # Low confidence forces the controller's conservative fallback.
        anomalous = item.timeout_rate > 0.8 or item.p95_latency_ms > 60_000
        return 0.1 if anomalous else 1.0
