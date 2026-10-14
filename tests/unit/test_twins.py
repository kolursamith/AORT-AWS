from datetime import UTC, datetime

import pytest

from contracts.v1.models import RouteTelemetry
from intelligence.twin.route_twin import RouteTwin


def telemetry(sequence: int) -> RouteTelemetry:
    return RouteTelemetry(
        experiment_id="exp-1",
        route_id="route-1",
        timestamp=datetime.now(UTC),
        payment_rps=1,
        check_rps=1,
        observed_throughput_rps=2,
        p95_latency_ms=5,
        timeout_rate=0,
        queue_depth=0,
        source_id="mock-switch",
        sequence=sequence,
    )


def test_route_twin_rejects_replay() -> None:
    twin = RouteTwin("route-1")
    twin.observe(telemetry(1))
    with pytest.raises(ValueError, match="stale"):
        twin.observe(telemetry(1))
