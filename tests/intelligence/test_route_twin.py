"""Stage 1 Route Digital Twin tests. Groups A–G follow the Stage 1 acceptance list.

All tests are deterministic: time comes from ManualClock, with no sleeps, network or I/O.
"""

import math
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from contracts.v1_1 import (
    ErrorCode,
    FreshnessStatus,
    RouteDescriptor,
    RouteTelemetry,
    SourceSequenceMark,
    ValidatedRouteTelemetry,
)
from intelligence.common import ManualClock
from intelligence.twin import (
    IngestStatus,
    RouteRegistrationConflictError,
    RouteTwinConfig,
    RouteTwinInvariantError,
    RouteTwinRegistry,
    UnknownRouteError,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
EXP1, EXP2 = "EXP-1", "EXP-2"
AB, AC = "A1-B", "A1-C"
PROBE, SECOND = "probe-A1", "probe-A1-secondary"

BASE_CONFIG = {
    "model_version": "route-twin-1",
    "max_observation_age_s": 30.0,
    "max_future_skew_s": 2.0,
    "freshness_threshold_s": 10.0,
}
# The illustrative Stage 1 observation: 80 payments/s, 5 checks/s, 75 completions/s ...
DEFAULT_MEASURES = {
    "payment_rps": 80.0,
    "check_rps": 5.0,
    "observed_throughput_rps": 75.0,
    "p50_latency_ms": 40.0,
    "p95_latency_ms": 120.0,
    "latency_sample_count": 75,
    "timeout_rate": 0.02,
    "queue_depth": 10,
}
ESTIMATE_FIELDS = [
    "estimated_capacity_rps",
    "capacity_lower_bound_rps",
    "estimated_queue_depth",
    "estimated_timeout_probability",
    "telemetry_confidence",
    "estimator_id",
]


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def make_config(**overrides) -> RouteTwinConfig:
    return RouteTwinConfig(**{**BASE_CONFIG, **overrides})


def descriptor(exp: str = EXP1, route: str = AB, beneficiary: str = "B", **overrides):
    return RouteDescriptor(
        **{
            "schema_version": "1.1",
            "experiment_id": exp,
            "route_id": route,
            "remitter_bank_id": "A1",
            "switch_id": "SW",
            "beneficiary_bank_id": beneficiary,
            **overrides,
        }
    )


def sample(
    seq: int,
    *,
    exp: str = EXP1,
    route: str = AB,
    source: str = PROBE,
    t: float | None = None,
    start: float | None = None,
    **measures,
) -> RouteTelemetry:
    """Telemetry for window [start, t), emitted at t. By default t = seq so windows chain."""

    end = float(seq) if t is None else t
    return RouteTelemetry(
        schema_version="1.1",
        experiment_id=exp,
        route_id=route,
        source_id=source,
        observation_scope="REMITTER_OWN_TRAFFIC",
        sequence=seq,
        observation_window_start=at(end - 1 if start is None else start),
        observation_window_end=at(end),
        observation_timestamp=at(end),
        **{**DEFAULT_MEASURES, **measures},
    )


def envelope(telemetry: RouteTelemetry, *, trust="AUTHENTICATED", flags=()):
    cross = "INCONSISTENT" if "CROSS_CHECK_DEVIATION" in flags else "CONSISTENT"
    return ValidatedRouteTelemetry(
        schema_version="1.1",
        experiment_id=telemetry.experiment_id,
        route_id=telemetry.route_id,
        telemetry=telemetry,
        trust_status=trust,
        validator_id="validator-B",
        validator_version="0.1",
        validated_at=telemetry.observation_timestamp + timedelta(milliseconds=100),
        validation_flags=tuple(flags),
        cross_check_status=cross,
    )


def item(seq: int, *, trust="AUTHENTICATED", flags=(), **sample_kwargs):
    return envelope(sample(seq, **sample_kwargs), trust=trust, flags=flags)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(at(5))


@pytest.fixture
def twin(clock) -> RouteTwinRegistry:
    registry = RouteTwinRegistry(make_config(), clock)
    registry.register_route(descriptor(route=AB, beneficiary="B"), [PROBE, SECOND])
    registry.register_route(descriptor(route=AC, beneficiary="C"), [PROBE])
    return registry


def snap(registry: RouteTwinRegistry, exp: str = EXP1, route: str = AB):
    return registry.snapshot(exp, route)


# ============================================================ Group A — registration


def test_a01_valid_registration_creates_an_empty_twin(clock):
    registry = RouteTwinRegistry(make_config(), clock)
    assert registry.register_route(descriptor(), [PROBE]) is True
    snapshot = snap(registry)
    assert snapshot.freshness_status is FreshnessStatus.NO_DATA
    assert snapshot.snapshot_version == 0
    assert snapshot.latest_trusted_observation is None
    assert snapshot.latest_flagged_observation is None
    assert snapshot.latest_sequence_per_source == ()
    assert snapshot.last_observed_at is None
    assert snapshot.last_updated_at == at(5)


def test_a02_identical_registration_is_idempotent(twin):
    twin.ingest(item(1))
    assert twin.register_route(descriptor(route=AB, beneficiary="B"), [SECOND, PROBE]) is False
    assert snap(twin).snapshot_version == 1
    assert snap(twin).latest_trusted_observation.sequence == 1


@pytest.mark.parametrize(
    ("changed_descriptor", "sources"),
    [
        (descriptor(route=AB, beneficiary="B", switch_id="SW-2"), [PROBE, SECOND]),
        (descriptor(route=AB, beneficiary="Z"), [PROBE, SECOND]),
        (descriptor(route=AB, beneficiary="B"), [PROBE]),
    ],
    ids=["different-switch", "different-beneficiary", "different-sources"],
)
def test_a03_conflicting_registration_is_rejected(twin, changed_descriptor, sources):
    with pytest.raises(RouteRegistrationConflictError):
        twin.register_route(changed_descriptor, sources)
    assert twin.ingest(item(1, source=SECOND)).applied


def test_a04_unregistered_route_ingestion_creates_no_state(twin):
    result = twin.ingest(item(1, route="A1-D"))
    assert result.status is IngestStatus.REJECTED_IDENTITY
    assert result.error_code is ErrorCode.ROUTE_IDENTITY_MISMATCH
    assert result.snapshot_version is None
    with pytest.raises(UnknownRouteError):
        twin.snapshot(EXP1, "A1-D")
    assert twin.registered_routes(EXP1) == (AB, AC)


def test_a05_same_route_id_in_two_experiments_is_independent(twin):
    twin.register_route(descriptor(exp=EXP2), [PROBE])
    assert twin.ingest(item(1)).applied
    assert snap(twin, EXP2).freshness_status is FreshnessStatus.NO_DATA
    assert snap(twin, EXP2).snapshot_version == 0
    assert snap(twin, EXP1).snapshot_version == 1


@pytest.mark.parametrize(
    ("source", "route"),
    [("rogue-probe", AB), (SECOND, AC)],
    ids=["never-registered", "registered-for-another-route"],
)
def test_a06_source_must_be_authorised_for_the_route(twin, source, route):
    result = twin.ingest(item(1, source=source, route=route))
    assert result.status is IngestStatus.REJECTED_SOURCE
    assert result.error_code is ErrorCode.UNKNOWN_SOURCE
    assert snap(twin, route=route).snapshot_version == 0


def test_a07_wrong_route_identity_cannot_alter_either_route(twin):
    telemetry = sample(1, route=AB)
    forged = ValidatedRouteTelemetry.model_construct(
        **{**envelope(telemetry).__dict__, "route_id": AC}
    )
    result = twin.ingest(forged)
    assert result.status is IngestStatus.REJECTED_IDENTITY
    assert result.error_code is ErrorCode.ROUTE_IDENTITY_MISMATCH
    assert snap(twin, route=AB).snapshot_version == 0
    assert snap(twin, route=AC).snapshot_version == 0


def test_a08_unknown_experiment_is_rejected(twin):
    result = twin.ingest(item(1, exp="EXP-9"))
    assert result.status is IngestStatus.REJECTED_IDENTITY
    assert result.error_code is ErrorCode.EXPERIMENT_MISMATCH


@pytest.mark.parametrize(
    ("sources", "error"),
    [
        ("probe-A1", TypeError),
        ([], ValueError),
        (["bad source"], ValidationError),
        ([""], ValidationError),
    ],
)
def test_a09_registration_validates_sources(clock, sources, error):
    registry = RouteTwinRegistry(make_config(), clock)
    with pytest.raises(error):
        registry.register_route(descriptor(), sources)
    assert registry.registered_routes(EXP1) == ()


def test_a10_registration_requires_a_descriptor(clock):
    registry = RouteTwinRegistry(make_config(), clock)
    with pytest.raises(TypeError):
        registry.register_route({"route_id": AB}, [PROBE])


# ============================================================ Group B — ingestion


def test_b01_trusted_telemetry_is_applied(twin):
    result = twin.ingest(item(1))
    assert result.status is IngestStatus.APPLIED
    assert result.applied and result.trusted
    assert result.error_code is None
    assert result.snapshot_version == 1


def test_b02_snapshot_reflects_the_observed_measurements(twin):
    telemetry = sample(1)
    twin.ingest(envelope(telemetry))
    observed = snap(twin).latest_trusted_observation
    assert observed == telemetry
    assert observed.payment_rps == 80.0
    assert observed.check_rps == 5.0
    assert observed.observed_throughput_rps == 75.0
    assert observed.p95_latency_ms == 120.0
    assert observed.timeout_rate == 0.02
    assert observed.queue_depth == 10
    assert snap(twin).last_observed_at == at(1)


NULL_MEASURES = {
    "observed_throughput_rps": None,
    "p50_latency_ms": None,
    "p95_latency_ms": None,
    "latency_sample_count": 0,
    "timeout_rate": None,
    "queue_depth": None,
}


def test_b03_null_observations_stay_null(twin):
    assert twin.ingest(item(1, **NULL_MEASURES)).applied
    observed = snap(twin).latest_trusted_observation
    for field in ("observed_throughput_rps", "p50_latency_ms", "timeout_rate", "queue_depth"):
        assert getattr(observed, field) is None


def test_b04_measured_zero_is_kept_as_zero(twin):
    zeros = {
        "payment_rps": 0.0,
        "check_rps": 0.0,
        "observed_throughput_rps": 0.0,
        "p50_latency_ms": None,
        "p95_latency_ms": None,
        "latency_sample_count": 0,
        "timeout_rate": 0.0,
        "queue_depth": 0,
    }
    twin.ingest(item(1, **zeros))
    observed = snap(twin).latest_trusted_observation
    assert observed.observed_throughput_rps == 0.0 and observed.observed_throughput_rps is not None
    assert observed.timeout_rate == 0.0 and observed.timeout_rate is not None
    assert observed.queue_depth == 0 and observed.queue_depth is not None


def test_b05_missing_latency_is_null_while_other_measures_remain(twin):
    twin.ingest(item(1, p50_latency_ms=None, p95_latency_ms=None, latency_sample_count=0))
    observed = snap(twin).latest_trusted_observation
    assert observed.p50_latency_ms is None and observed.p95_latency_ms is None
    assert observed.payment_rps == 80.0


def test_b06_input_telemetry_is_not_modified(twin):
    incoming = item(1)
    before = incoming.model_dump_json()
    twin.ingest(incoming)
    assert incoming.model_dump_json() == before


@pytest.mark.parametrize(
    "flags",
    [("STATISTICAL_ANOMALY",), ("CROSS_CHECK_DEVIATION",), ("SEQUENCE_GAP", "CLOCK_SKEW")],
)
def test_b07_authenticated_flagged_sample_is_kept_separate(twin, flags):
    result = twin.ingest(item(1, flags=flags))
    assert result.applied and not result.trusted
    snapshot = snap(twin)
    assert snapshot.latest_trusted_observation is None
    assert snapshot.latest_flagged_observation.validation_flags == flags
    assert snapshot.freshness_status is FreshnessStatus.STALE


def test_b08_flagged_sample_does_not_overwrite_trusted_measurement(twin):
    twin.ingest(item(1))
    inflated = item(2, flags=("STATISTICAL_ANOMALY",), observed_throughput_rps=5000.0)
    assert twin.ingest(inflated).applied
    snapshot = snap(twin)
    assert snapshot.latest_trusted_observation.sequence == 1
    assert snapshot.latest_trusted_observation.observed_throughput_rps == 75.0
    assert snapshot.latest_flagged_observation.telemetry.observed_throughput_rps == 5000.0
    assert snapshot.estimated_capacity_rps is None


@pytest.mark.parametrize("trust", ["UNAUTHENTICATED", "UNKNOWN_SOURCE"])
@pytest.mark.parametrize("flags", [(), ("STATISTICAL_ANOMALY",)])
def test_b09_unauthenticated_sample_is_rejected(twin, trust, flags):
    result = twin.ingest(item(1, trust=trust, flags=flags))
    assert result.status is IngestStatus.REJECTED_TRUST
    assert result.error_code is ErrorCode.UNTRUSTED_SOURCE
    snapshot = snap(twin)
    assert snapshot.latest_trusted_observation is None
    assert snapshot.latest_flagged_observation is None
    assert twin.source_status(EXP1, PROBE).last_sequence is None


def test_b10_inconsistent_envelope_cannot_alter_state(twin):
    telemetry = sample(1)
    with pytest.raises(ValidationError, match="EXPERIMENT_MISMATCH"):
        ValidatedRouteTelemetry(**{**envelope(telemetry).__dict__, "experiment_id": EXP2})
    forged = ValidatedRouteTelemetry.model_construct(
        **{**envelope(telemetry).__dict__, "experiment_id": EXP2}
    )
    result = twin.ingest(forged)
    assert result.status is IngestStatus.REJECTED_IDENTITY
    assert result.error_code is ErrorCode.EXPERIMENT_MISMATCH
    assert snap(twin).snapshot_version == 0


def test_b11_forged_envelope_cannot_inject_into_another_registered_experiment(twin):
    # Without the twin's own identity re-check, EXP-1 telemetry would land in EXP-2's route.
    twin.register_route(descriptor(exp=EXP2), [PROBE])
    forged = ValidatedRouteTelemetry.model_construct(
        **{**envelope(sample(1, exp=EXP1)).__dict__, "experiment_id": EXP2}
    )
    result = twin.ingest(forged)
    assert result.status is IngestStatus.REJECTED_IDENTITY
    assert "differs from telemetry" in result.detail
    assert snap(twin, EXP1).snapshot_version == 0
    assert snap(twin, EXP2).snapshot_version == 0


def test_b12_older_sample_from_another_source_does_not_replace_newer_trusted(twin):
    twin.ingest(item(4, source=PROBE))  # observed at t=4
    result = twin.ingest(item(3, source=SECOND))  # observed at t=3, applied later
    assert result.applied
    snapshot = snap(twin)
    assert snapshot.latest_trusted_observation.source_id == PROBE
    assert snapshot.latest_trusted_observation.observation_timestamp == at(4)
    assert snapshot.latest_sequence_per_source[1] == SourceSequenceMark(
        source_id=SECOND, sequence=3
    )
    assert snapshot.snapshot_version == 2


# ============================================================ Group C — sequences


def test_c01_initial_sequence_is_applied_without_a_gap(twin):
    result = twin.ingest(item(1))
    assert result.applied and result.missing_sequences == 0
    assert twin.source_status(EXP1, PROBE).last_sequence == 1


def test_c02_next_sequence_is_applied(twin):
    twin.ingest(item(1))
    result = twin.ingest(item(2, payment_rps=90.0))
    assert result.applied and result.snapshot_version == 2
    assert snap(twin).latest_sequence_per_source == (
        SourceSequenceMark(source_id=PROBE, sequence=2),
    )
    assert snap(twin).latest_trusted_observation.payment_rps == 90.0


def test_c03_identical_duplicate_is_reported(twin):
    twin.ingest(item(1))
    result = twin.ingest(item(1))
    assert result.status is IngestStatus.DUPLICATE
    assert result.error_code is ErrorCode.DUPLICATE_SEQUENCE
    assert not result.applied


def test_c04_duplicate_changes_nothing(twin):
    twin.ingest(item(1))
    before, source_before = snap(twin), twin.source_status(EXP1, PROBE)
    twin.ingest(item(1))
    assert snap(twin) == before
    assert twin.source_status(EXP1, PROBE) == source_before


def test_c05_same_sequence_with_different_payload_is_a_conflict(twin):
    twin.ingest(item(1))
    result = twin.ingest(item(1, payment_rps=999.0))
    assert result.status is IngestStatus.CONFLICT
    assert result.error_code is ErrorCode.CONFLICTING_SEQUENCE
    assert snap(twin).latest_trusted_observation.payment_rps == 80.0
    assert twin.source_status(EXP1, PROBE).quarantined


def test_c06_lower_unseen_sequence_is_out_of_order(twin):
    twin.ingest(item(3))
    result = twin.ingest(item(2))
    assert result.status is IngestStatus.OUT_OF_ORDER
    assert result.error_code is ErrorCode.OUT_OF_ORDER_SEQUENCE
    assert snap(twin).latest_trusted_observation.sequence == 3
    assert not twin.source_status(EXP1, PROBE).quarantined


def test_c07_higher_sequence_with_gap_is_applied_and_recorded(twin):
    twin.ingest(item(1))
    result = twin.ingest(item(4))
    assert result.applied and result.sequence_gap
    assert result.missing_sequences == 2
    status = twin.source_status(EXP1, PROBE)
    assert (status.gaps_detected, status.missing_sequences_total) == (1, 2)


def test_c08_one_source_sequence_spans_routes(twin, clock):
    clock.set_time(at(13))
    results = [
        twin.ingest(item(11, route=AB)),
        twin.ingest(item(12, route=AC)),
        twin.ingest(item(13, route=AB)),
    ]
    assert [r.status for r in results] == [IngestStatus.APPLIED] * 3
    assert [r.missing_sequences for r in results] == [0, 0, 0]
    assert snap(twin, route=AB).latest_sequence_per_source[0].sequence == 13
    assert snap(twin, route=AC).latest_sequence_per_source[0].sequence == 12
    assert twin.source_status(EXP1, PROBE).last_sequence == 13


def test_c08b_lower_sequence_on_another_route_is_out_of_order(twin, clock):
    clock.set_time(at(13))
    twin.ingest(item(13, route=AB))
    result = twin.ingest(item(12, route=AC))
    assert result.status is IngestStatus.OUT_OF_ORDER


def test_c09_duplicate_after_a_later_sequence_is_still_a_duplicate(twin):
    twin.ingest(item(1))
    twin.ingest(item(2))
    result = twin.ingest(item(1))
    assert result.status is IngestStatus.DUPLICATE
    assert snap(twin).latest_trusted_observation.sequence == 2


def test_c10_concurrent_duplicates_apply_exactly_once(twin):
    incoming = item(1)
    barrier = threading.Barrier(32)

    def deliver():
        barrier.wait()
        return twin.ingest(incoming).status

    with ThreadPoolExecutor(max_workers=32) as pool:
        statuses = list(pool.map(lambda _: deliver(), range(32)))
    assert statuses.count(IngestStatus.APPLIED) == 1
    assert statuses.count(IngestStatus.DUPLICATE) == 31
    assert snap(twin).snapshot_version == 1


def test_c11_lock_serialises_duplicate_check_and_application(twin, monkeypatch):
    """Make both deliveries try to meet after passing the duplicate check.

    ``_SourceState.remember`` runs only once a delivery has passed the duplicate check. With
    the lock, the second delivery is blocked outside, so the first one's rendezvous times out
    and the second is then reported as a duplicate. Without the lock, both would meet there
    and both would apply.
    """

    import intelligence.twin.route_twin as route_twin_module

    original = route_twin_module._SourceState.remember
    rendezvous = threading.Barrier(2, timeout=0.5)

    def remember_after_rendezvous(self, sequence, fingerprint):
        try:
            rendezvous.wait()
        except threading.BrokenBarrierError:
            pass
        return original(self, sequence, fingerprint)

    monkeypatch.setattr(route_twin_module._SourceState, "remember", remember_after_rendezvous)
    incoming = item(1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: twin.ingest(incoming).status, range(2)))
    assert sorted(statuses) == sorted([IngestStatus.APPLIED, IngestStatus.DUPLICATE])
    assert snap(twin).snapshot_version == 1


# ============================================================ Group D — time


def test_d01_utc_timestamps_are_kept_in_utc(twin):
    twin.ingest(item(1))
    snapshot = snap(twin)
    assert snapshot.last_observed_at == at(1)
    assert snapshot.last_observed_at.utcoffset() == timedelta(0)
    assert snapshot.last_updated_at.utcoffset() == timedelta(0)


def test_d02_non_utc_aware_timestamps_are_normalised(twin):
    ist = timezone(timedelta(hours=5, minutes=30))
    telemetry = RouteTelemetry(
        **{
            **sample(1).model_dump(),
            "observation_window_start": at(0).astimezone(ist),
            "observation_window_end": at(1).astimezone(ist),
            "observation_timestamp": at(1).astimezone(ist),
        }
    )
    assert twin.ingest(envelope(telemetry)).applied
    observed = snap(twin).latest_trusted_observation
    assert observed.observation_timestamp == at(1)
    assert observed.observation_timestamp.utcoffset() == timedelta(0)


def test_d03_naive_timestamps_are_rejected_before_ingestion():
    with pytest.raises(ValidationError, match="timezone"):
        RouteTelemetry(
            **{**sample(1).model_dump(), "observation_timestamp": datetime(2026, 3, 1, 12, 0, 1)}
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        ManualClock(datetime(2026, 3, 1, 12, 0))


def test_d04_stale_observation_is_rejected(twin):
    result = twin.ingest(item(1, t=-30))
    assert result.status is IngestStatus.STALE
    assert result.error_code is ErrorCode.STALE_OBSERVATION
    assert snap(twin).snapshot_version == 0


def test_d05_future_observation_is_rejected(twin):
    result = twin.ingest(item(1, t=8))
    assert result.status is IngestStatus.FUTURE
    assert result.error_code is ErrorCode.FUTURE_TIMESTAMP
    assert snap(twin).snapshot_version == 0


@pytest.mark.parametrize("t", [-25.0, 7.0], ids=["age-exactly-max", "skew-exactly-max"])
def test_d06_timestamps_exactly_at_the_limits_are_accepted(twin, t):
    assert twin.ingest(item(1, t=t)).applied


@pytest.mark.parametrize(
    ("t", "status"),
    [(-25.001, IngestStatus.STALE), (7.001, IngestStatus.FUTURE)],
    ids=["just-too-old", "just-too-far-ahead"],
)
def test_d07_timestamps_just_outside_the_limits_are_rejected(twin, t, status):
    assert twin.ingest(item(1, t=t)).status is status


def test_d08_overlapping_windows_from_one_source_are_a_conflict(twin):
    twin.ingest(item(1))  # window [0, 1)
    result = twin.ingest(item(2, start=0.5))  # window [0.5, 2)
    assert result.status is IngestStatus.CONFLICT
    assert "overlaps" in result.detail
    assert snap(twin).latest_trusted_observation.sequence == 1


def test_d08b_adjacent_and_cross_route_windows_are_allowed(twin):
    assert twin.ingest(item(1, route=AB)).applied  # [0, 1)
    assert twin.ingest(item(2, route=AB)).applied  # [1, 2): half-open, adjacent
    assert twin.ingest(item(3, route=AC, start=0)).applied  # overlaps AB's windows: other route


def test_d09_manual_clock_rejects_backward_time():
    clock = ManualClock(at(10))
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(-1)
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(timedelta(seconds=-1))
    with pytest.raises(ValueError, match="backwards"):
        clock.set_time(at(9))
    with pytest.raises(ValueError, match="finite"):
        clock.advance(math.nan)
    with pytest.raises(TypeError):
        clock.advance(True)
    assert clock.now() == at(10)


def test_d10_manual_clock_advances_deterministically():
    first, second = ManualClock(at(0)), ManualClock(at(0))
    for clock in (first, second):
        clock.advance(1.5)
        clock.advance(timedelta(seconds=2))
        clock.advance(0)
        clock.set_time(at(3.5))
    assert first.now() == second.now() == at(3.5)
    assert first.now().tzinfo is UTC


def test_d11_twin_rejects_a_clock_that_moves_backwards():
    class RegressingClock:
        def __init__(self):
            self.times = [at(5), at(4)]

        def now(self):
            return self.times.pop(0)

    registry = RouteTwinRegistry(make_config(), RegressingClock())
    registry.register_route(descriptor(), [PROBE])
    with pytest.raises(RouteTwinInvariantError, match="backwards"):
        registry.snapshot(EXP1, AB)


def test_d12_twin_rejects_a_naive_clock():
    class NaiveClock:
        def now(self):
            return datetime(2026, 3, 1, 12, 0)

    registry = RouteTwinRegistry(make_config(), NaiveClock())
    with pytest.raises(RouteTwinInvariantError, match="timezone-aware"):
        registry.register_route(descriptor(), [PROBE])


# ============================================================ Group E — snapshots


def test_e01_e02_snapshot_identity(twin):
    twin.register_route(descriptor(exp=EXP2, route=AC, beneficiary="C"), [PROBE])
    snapshot = snap(twin, EXP2, AC)
    assert (snapshot.experiment_id, snapshot.route_id) == (EXP2, AC)
    assert snapshot.model_version == "route-twin-1"


def test_e03_version_increases_with_each_applied_observation(twin):
    versions = [snap(twin).snapshot_version]
    for seq in (1, 2, 3):
        twin.ingest(item(seq))
        versions.append(snap(twin).snapshot_version)
    assert versions == [0, 1, 2, 3]


def test_e04_version_unchanged_after_duplicate(twin):
    twin.ingest(item(1))
    twin.ingest(item(1))
    assert snap(twin).snapshot_version == 1


REJECTIONS = {
    "stale": lambda tw: tw.ingest(item(9, t=-40)),
    "future": lambda tw: tw.ingest(item(9, t=9)),
    "untrusted": lambda tw: tw.ingest(item(9, trust="UNAUTHENTICATED", t=3)),
    "unknown-source": lambda tw: tw.ingest(item(9, source="rogue", t=3)),
    "out-of-order": lambda tw: tw.ingest(item(1, t=0.5, start=0)),
    "conflict": lambda tw: tw.ingest(item(2, payment_rps=1.0)),
}


@pytest.mark.parametrize("kind", list(REJECTIONS))
def test_e05_version_unchanged_after_rejected_input(twin, kind):
    twin.ingest(item(2))
    before = snap(twin)
    result = REJECTIONS[kind](twin)
    assert not result.applied
    after = snap(twin)
    assert after.snapshot_version == before.snapshot_version == 1
    assert after.latest_trusted_observation == before.latest_trusted_observation


def test_e06_previous_snapshots_never_change(twin):
    twin.ingest(item(1))
    earlier = snap(twin)
    twin.ingest(item(2, payment_rps=200.0))
    twin.ingest(item(3, flags=("STATISTICAL_ANOMALY",)))
    assert earlier.snapshot_version == 1
    assert earlier.latest_trusted_observation.payment_rps == 80.0
    assert earlier.latest_flagged_observation is None
    with pytest.raises(ValidationError, match="frozen"):
        earlier.snapshot_version = 99
    assert isinstance(earlier.latest_sequence_per_source, tuple)


def test_e07_sequence_watermarks_are_per_source_and_sorted(twin):
    twin.ingest(item(2, source=PROBE))
    twin.ingest(item(5, source=SECOND))
    assert snap(twin).latest_sequence_per_source == (
        SourceSequenceMark(source_id=PROBE, sequence=2),
        SourceSequenceMark(source_id=SECOND, sequence=5),
    )


def test_e08_assumptions_are_labelled_and_never_measured(clock):
    registry = RouteTwinRegistry(
        make_config(assumed_configured_capacity_rps=1000.0, assumed_client_timeout_s=15.0), clock
    )
    registry.register_route(descriptor(), [PROBE])
    empty = snap(registry)
    assert empty.assumed_configured_capacity_rps == 1000.0
    assert empty.latest_trusted_observation is None
    assert empty.estimated_capacity_rps is None
    registry.ingest(item(1))
    snapshot = snap(registry)
    assert snapshot.latest_trusted_observation.observed_throughput_rps == 75.0
    assert snapshot.assumed_client_timeout_s == 15.0
    assert snapshot.estimated_capacity_rps is None


def test_e08b_no_assumptions_unless_configured(twin):
    assert snap(twin).assumed_configured_capacity_rps is None
    assert snap(twin).assumed_client_timeout_s is None


@pytest.mark.parametrize("field", ESTIMATE_FIELDS)
def test_e09_e10_estimates_stay_null(twin, field):
    twin.ingest(item(1))
    twin.ingest(item(2, flags=("STATISTICAL_ANOMALY",), observed_throughput_rps=9000.0))
    twin.ingest(item(3, observed_throughput_rps=500.0))
    assert getattr(snap(twin), field) is None


def test_e11_no_data_until_an_observation_is_applied(twin):
    assert snap(twin).freshness_status is FreshnessStatus.NO_DATA
    twin.ingest(item(1, trust="UNAUTHENTICATED"))
    twin.ingest(item(1, t=-40))
    assert snap(twin).freshness_status is FreshnessStatus.NO_DATA


def test_e12_fresh_after_a_trusted_observation(twin):
    twin.ingest(item(1))
    assert snap(twin).freshness_status is FreshnessStatus.FRESH


def test_e13_stale_once_the_threshold_passes_without_losing_data(twin, clock):
    twin.ingest(item(1))  # observed at t=1
    clock.set_time(at(11))  # age exactly 10 s = threshold
    assert snap(twin).freshness_status is FreshnessStatus.FRESH
    clock.set_time(at(11.001))
    stale = snap(twin)
    assert stale.freshness_status is FreshnessStatus.STALE
    assert stale.latest_trusted_observation.payment_rps == 80.0
    assert stale.snapshot_version == 1


def test_e14_flagged_observation_does_not_refresh_trusted_freshness(twin, clock):
    twin.ingest(item(1))
    clock.set_time(at(20))
    assert twin.ingest(item(20, flags=("STATISTICAL_ANOMALY",))).applied
    snapshot = snap(twin)
    assert snapshot.freshness_status is FreshnessStatus.STALE
    assert snapshot.latest_trusted_observation.observation_timestamp == at(1)
    assert snapshot.last_observed_at == at(20)  # contract: latest of any held observation


def test_e15_duplicate_does_not_refresh_freshness(twin, clock):
    twin.ingest(item(1))
    clock.set_time(at(12))  # trusted sample is now 11 s old: stale
    result = twin.ingest(item(1))
    assert result.status is IngestStatus.DUPLICATE
    assert snap(twin).freshness_status is FreshnessStatus.STALE


# ============================================================ Group F — isolation, replay


def test_f01_two_experiments_are_isolated(twin):
    twin.register_route(descriptor(exp=EXP2), [PROBE])
    for seq in (1, 2, 3):
        twin.ingest(item(seq))
    result = twin.ingest(item(1, exp=EXP2, payment_rps=1.0))
    assert result.applied and result.missing_sequences == 0
    assert snap(twin, EXP1).latest_trusted_observation.payment_rps == 80.0
    assert snap(twin, EXP2).latest_trusted_observation.payment_rps == 1.0


def test_f02_two_routes_are_isolated(twin):
    twin.ingest(item(1, route=AB))
    assert snap(twin, route=AC).snapshot_version == 0
    assert snap(twin, route=AC).freshness_status is FreshnessStatus.NO_DATA


def _replay_script():
    clock = ManualClock(at(5))
    registry = RouteTwinRegistry(make_config(), clock)
    registry.register_route(descriptor(route=AB, beneficiary="B"), [PROBE, SECOND])
    registry.register_route(descriptor(route=AC, beneficiary="C"), [PROBE])
    statuses = []
    script = [
        item(1),
        item(1),
        item(2, route=AC),
        item(4, flags=("STATISTICAL_ANOMALY",)),
        item(3, payment_rps=50.0),
        item(5, source=SECOND, t=5),
        item(9, t=-40),
    ]
    for step, incoming in enumerate(script):
        clock.advance(0.25 * step)
        statuses.append(registry.ingest(incoming).status)
    return statuses, [registry.snapshot(EXP1, r) for r in (AB, AC)]


def test_f03_replay_produces_identical_measurements():
    (s1, snaps1), (s2, snaps2) = _replay_script(), _replay_script()
    assert s1 == s2
    assert [s.latest_trusted_observation for s in snaps1] == [
        s.latest_trusted_observation for s in snaps2
    ]
    assert [s.model_dump_json() for s in snaps1] == [s.model_dump_json() for s in snaps2]


def test_f04_replay_produces_identical_watermarks():
    (_, snaps1), (_, snaps2) = _replay_script(), _replay_script()
    assert [s.latest_sequence_per_source for s in snaps1] == [
        s.latest_sequence_per_source for s in snaps2
    ]


def test_f05_replay_produces_identical_versions_and_statuses():
    statuses, snaps = _replay_script()
    assert statuses == [
        IngestStatus.APPLIED,
        IngestStatus.DUPLICATE,
        IngestStatus.APPLIED,
        IngestStatus.APPLIED,
        IngestStatus.OUT_OF_ORDER,
        IngestStatus.APPLIED,
        IngestStatus.STALE,
    ]
    assert [s.snapshot_version for s in snaps] == [3, 1]
    assert [s.snapshot_version for s in _replay_script()[1]] == [3, 1]


def test_f06_no_dependence_on_the_real_clock():
    past = datetime(2001, 1, 1, tzinfo=UTC)
    registry = RouteTwinRegistry(make_config(), ManualClock(past + timedelta(seconds=1)))
    registry.register_route(descriptor(), [PROBE])
    telemetry = RouteTelemetry(
        **{
            **sample(1).model_dump(),
            "observation_window_start": past,
            "observation_window_end": past + timedelta(seconds=1),
            "observation_timestamp": past + timedelta(seconds=1),
        }
    )
    assert registry.ingest(envelope(telemetry)).applied
    assert registry.snapshot(EXP1, AB).freshness_status is FreshnessStatus.FRESH
    for module in ("intelligence/twin/route_twin.py", "intelligence/common/clock.py"):
        source = (REPO_ROOT / module).read_text()
        for forbidden in ("datetime.now(", "datetime.utcnow(", "time.time(", "time.monotonic("):
            assert forbidden not in source, f"{module} reads the system clock"


def test_f07_the_twin_exposes_no_financial_operations():
    public = {name for name in dir(RouteTwinRegistry) if not name.startswith("_")}
    assert public == {
        "config",
        "ingest",
        "register_route",
        "registered_routes",
        "release_source_quarantine",
        "snapshot",
        "source_status",
    }


def test_f08_importing_the_twin_loads_no_backend_or_io_modules():
    code = (
        "import sys, intelligence.twin, intelligence.common\n"
        "bad = [m for m in sys.modules if m.split('.')[0] in "
        "{'backend', 'sqlalchemy', 'psycopg', 'boto3', 'botocore', 'httpx', 'requests'}]\n"
        "print(','.join(sorted(bad)))\n"
    )
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and constant code
        [sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    assert completed.stdout.strip() == ""
    source = (REPO_ROOT / "intelligence/twin/route_twin.py").read_text()
    assert "import backend" not in source and "from backend" not in source


def test_f09_no_cross_experiment_leakage(twin):
    twin.register_route(descriptor(exp=EXP2), [PROBE])
    twin.ingest(item(1))
    twin.ingest(item(2, flags=("STATISTICAL_ANOMALY",)))
    other = snap(twin, EXP2)
    assert other.latest_sequence_per_source == ()
    assert other.latest_flagged_observation is None
    assert twin.source_status(EXP2, PROBE).last_sequence is None
    assert twin.source_status(EXP1, PROBE).last_sequence == 2


# ============================================================ Group G — robustness


@pytest.mark.parametrize(
    "overrides",
    [
        {"freshness_threshold_s": 31.0},
        {"model_version": "bad version!"},
        {"model_version": ""},
        {"max_observation_age_s": 0.0},
        {"recent_sequence_window": 0},
        {"unexpected": 1},
        {"max_future_skew_s": "2"},
    ],
)
def test_g01_invalid_configuration_is_rejected(overrides):
    with pytest.raises(ValidationError):
        make_config(**overrides)


@pytest.mark.parametrize(
    "field",
    [
        "max_observation_age_s",
        "max_future_skew_s",
        "freshness_threshold_s",
        "assumed_configured_capacity_rps",
        "assumed_client_timeout_s",
    ],
)
def test_g02_negative_durations_are_rejected(field):
    with pytest.raises(ValidationError, match=field):
        make_config(**{field: -1.0})


@pytest.mark.parametrize("value", [math.nan, math.inf])
@pytest.mark.parametrize(
    "field",
    [
        "max_observation_age_s",
        "max_future_skew_s",
        "freshness_threshold_s",
        "assumed_configured_capacity_rps",
    ],
)
def test_g03_non_finite_configuration_is_rejected(field, value):
    with pytest.raises(ValidationError, match=field):
        make_config(**{field: value})


def test_g04_unknown_route_snapshot_raises_without_creating_state(twin):
    with pytest.raises(UnknownRouteError):
        twin.snapshot(EXP2, AB)
    with pytest.raises(LookupError):
        twin.source_status(EXP2, PROBE)
    assert twin.registered_routes(EXP2) == ()


@pytest.mark.parametrize(
    "conflicting",
    [
        lambda: item(1, observed_throughput_rps=9999.0),
        lambda: item(2, start=0.25),
    ],
    ids=["reused-sequence", "overlapping-window"],
)
def test_g05_conflicting_telemetry_cannot_overwrite_the_last_good_sample(twin, conflicting):
    twin.ingest(item(1))
    good = snap(twin).latest_trusted_observation
    assert twin.ingest(conflicting()).status is IngestStatus.CONFLICT
    assert snap(twin).latest_trusted_observation == good


def test_g06_malformed_data_fails_before_ingestion(twin):
    with pytest.raises(ValidationError):
        sample(1, payment_rps=-5.0)
    with pytest.raises(TypeError):
        twin.ingest({"telemetry": "not a model"})
    with pytest.raises(TypeError):
        twin.ingest(sample(1))
    assert snap(twin).snapshot_version == 0


def test_g07_concurrent_ingestion_keeps_state_consistent(clock):
    sources = [f"probe-{n}" for n in range(8)]
    registry = RouteTwinRegistry(make_config(), clock)
    registry.register_route(descriptor(), sources)
    clock.set_time(at(25))
    observed_versions: list[int] = []
    done = threading.Event()

    def produce(source):
        return [registry.ingest(item(seq, source=source)).status for seq in range(1, 26)]

    def read():
        while not done.is_set():
            observed_versions.append(registry.snapshot(EXP1, AB).snapshot_version)

    with ThreadPoolExecutor(max_workers=1) as reader_pool:
        reader = reader_pool.submit(read)
        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(produce, sources))
        done.set()
        reader.result()  # re-raises anything the reader hit, e.g. an inconsistent snapshot

    assert observed_versions, "the reader never observed a snapshot"
    assert all(status is IngestStatus.APPLIED for run in outcomes for status in run)
    final = registry.snapshot(EXP1, AB)
    assert final.snapshot_version == 200
    assert {mark.sequence for mark in final.latest_sequence_per_source} == {25}
    assert observed_versions == sorted(observed_versions)


def test_g08_repeated_snapshots_do_not_mutate_state(twin):
    twin.ingest(item(1))
    snapshots = [snap(twin) for _ in range(50)]
    assert all(s == snapshots[0] for s in snapshots)
    assert snap(twin).snapshot_version == 1


def test_g09_ingestion_continues_after_rejection_unless_quarantined(twin):
    assert twin.ingest(item(1, t=-40)).status is IngestStatus.STALE
    assert twin.ingest(item(1)).applied
    assert twin.ingest(item(1, payment_rps=7.0)).status is IngestStatus.CONFLICT
    blocked = twin.ingest(item(2))
    assert blocked.status is IngestStatus.REJECTED_SOURCE
    assert blocked.error_code is ErrorCode.UNTRUSTED_SOURCE
    assert twin.release_source_quarantine(EXP1, PROBE) is True
    assert twin.ingest(item(2)).applied
    assert twin.release_source_quarantine(EXP1, PROBE) is False


def test_g10_accepted_gaps_are_visibly_recorded(twin):
    twin.ingest(item(1))
    gap = twin.ingest(item(5))
    assert gap.applied and gap.sequence_gap and gap.missing_sequences == 3
    assert "3 sequence(s) missing" in gap.detail
    status = twin.source_status(EXP1, PROBE)
    assert status.gaps_detected == 1 and status.missing_sequences_total == 3
    assert snap(twin).latest_sequence_per_source[0].sequence == 5


def test_g11_recent_sequence_window_bounds_duplicate_memory(clock):
    registry = RouteTwinRegistry(make_config(recent_sequence_window=2), clock)
    registry.register_route(descriptor(), [PROBE])
    for seq in (1, 2, 3):
        registry.ingest(item(seq))
    assert registry.ingest(item(3)).status is IngestStatus.DUPLICATE
    # Sequence 1 has left the duplicate window, so a redelivery is reported as out of order.
    assert registry.ingest(item(1)).status is IngestStatus.OUT_OF_ORDER
