"""Smoke test: the Route Twin never re-applies replayed or older telemetry.

The full behaviour matrix lives in tests/intelligence/test_route_twin.py; this keeps the original
starter's intent (replay rejection) against the real Track A Route Twin.
"""

from datetime import UTC, datetime, timedelta

from contracts.v1_1 import RouteDescriptor, RouteTelemetry, ValidatedRouteTelemetry
from intelligence.common import ManualClock
from intelligence.twin import IngestStatus, RouteTwinConfig, RouteTwinRegistry

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def envelope(sequence: int, payment_rps: float = 1.0) -> ValidatedRouteTelemetry:
    end = T0 + timedelta(seconds=sequence)
    telemetry = RouteTelemetry(
        schema_version="1.1",
        experiment_id="exp-1",
        route_id="route-1",
        source_id="mock-switch",
        observation_scope="REMITTER_OWN_TRAFFIC",
        sequence=sequence,
        observation_window_start=end - timedelta(seconds=1),
        observation_window_end=end,
        observation_timestamp=end,
        payment_rps=payment_rps,
        check_rps=1.0,
        observed_throughput_rps=2.0,
        p50_latency_ms=None,
        p95_latency_ms=5.0,
        latency_sample_count=2,
        timeout_rate=0.0,
        queue_depth=0,
    )
    return ValidatedRouteTelemetry(
        schema_version="1.1",
        experiment_id="exp-1",
        route_id="route-1",
        telemetry=telemetry,
        trust_status="AUTHENTICATED",
        validator_id="validator",
        validator_version="1",
        validated_at=end,
        cross_check_status="CONSISTENT",
    )


def test_route_twin_rejects_replay() -> None:
    twin = RouteTwinRegistry(
        RouteTwinConfig(
            model_version="smoke-1",
            max_observation_age_s=60.0,
            max_future_skew_s=1.0,
            freshness_threshold_s=10.0,
        ),
        ManualClock(T0 + timedelta(seconds=5)),
    )
    twin.register_route(
        RouteDescriptor(
            schema_version="1.1",
            experiment_id="exp-1",
            route_id="route-1",
            remitter_bank_id="A1",
            switch_id="SW",
            beneficiary_bank_id="B",
        ),
        ["mock-switch"],
    )
    assert twin.ingest(envelope(2)).applied
    assert twin.ingest(envelope(2)).status is IngestStatus.DUPLICATE
    assert twin.ingest(envelope(1, payment_rps=9.0)).status is IngestStatus.OUT_OF_ORDER
    assert twin.snapshot("exp-1", "route-1").snapshot_version == 1
