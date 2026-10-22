"""Stage 06: trust-aware capacity estimator, driven through the real Route Twin.

Groups: A mathematical correctness, B input integrity and poisoning, C system boundaries,
E security integration. Every sample reaches the estimator as a ``RouteTwinSnapshot`` produced
by ``RouteTwinRegistry`` (except where a test deliberately forges a snapshot that bypassed
contract validation). Time comes from ``ManualClock``; nothing sleeps or touches the network.
"""

import ast
import math
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from contracts.v1_1 import (
    SCHEMA_VERSION,
    RouteDescriptor,
    RouteTelemetry,
    RouteTwinSnapshot,
    ValidatedRouteTelemetry,
)
from intelligence.common.clock import ManualClock
from intelligence.ml.capacity_estimator import (
    CapacityEstimate,
    CapacityEstimator,
    CapacityEstimatorConfig,
    EstimatorInputError,
    EstimatorMode,
    Identifiability,
    Reason,
    RouteState,
    annotate_snapshot,
    ewma_alpha,
    ewma_update,
    max_block_mean,
    median,
    nearest_rank,
    rate_limited,
)
from intelligence.twin.route_twin import IngestStatus, RouteTwinConfig, RouteTwinRegistry

REPO_ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 4, 1, 9, 0, tzinfo=UTC)
EXP, ROUTE, SOURCE = "EXP-CAP", "A1-B", "src-A1-B"

_CFG = CapacityEstimatorConfig()
LAG = 1.0 - _CFG.lag_allowance_s / _CFG.sustain_s  # response-lag allowance on served work
HEALTHY = dict(payment_rps=35.0, throughput=35.0, p95_ms=300.0, timeouts=0, queue=5)
CONGESTED = dict(payment_rps=35.0, throughput=20.0, p95_ms=9000.0, timeouts=8, queue=300)


class Feed:
    """One route, registered sources, 1 s windows: telemetry -> twin -> snapshot -> estimator."""

    def __init__(self, config=None, experiment=EXP, route=ROUTE, sources=None, twin=None,
                 clock=None, register=True):  # fmt: skip
        sources = sources or (SOURCE if route == ROUTE else f"src-{route}",)
        self.sources = tuple(sources)
        self.clock = clock or ManualClock(T0)
        self.twin = twin or RouteTwinRegistry(
            RouteTwinConfig(model_version="rt-1", max_observation_age_s=30.0,
                            max_future_skew_s=1.0, freshness_threshold_s=5.0),
            self.clock,
        )  # fmt: skip
        self.experiment, self.route = experiment, route
        if register:
            self.twin.register_route(
                RouteDescriptor(schema_version=SCHEMA_VERSION, experiment_id=experiment,
                                route_id=route, remitter_bank_id="A1", switch_id="SW",
                                beneficiary_bank_id="B"),
                sources,
            )  # fmt: skip
        self.estimator = CapacityEstimator(experiment, route, config)
        self.k = 0
        self.seq = {s: -1 for s in sources}
        self.ingests = []

    def telemetry(self, k, source=None, *, payment_rps=35.0, throughput=35.0, p95_ms=300.0,
                  timeouts=0, queue=5, latency_samples=None, check_rps=0.0, seq=None,
                  scope="REMITTER_OWN_TRAFFIC"):  # fmt: skip
        source = source or self.sources[0]
        start = T0 + timedelta(seconds=k)
        responses = int(round(throughput)) if math.isfinite(throughput) else 0
        determined = responses + timeouts
        samples = responses if latency_samples is None else latency_samples
        return RouteTelemetry(
            schema_version=SCHEMA_VERSION, experiment_id=self.experiment, route_id=self.route,
            source_id=source, observation_scope=scope,
            sequence=self.seq[source] + 1 if seq is None else seq,
            observation_window_start=start, observation_window_end=start + timedelta(seconds=1),
            observation_timestamp=start + timedelta(seconds=1),
            payment_rps=payment_rps, check_rps=check_rps, observed_throughput_rps=throughput,
            p50_latency_ms=None if p95_ms is None or samples == 0 else p95_ms / 2,
            p95_latency_ms=None if samples == 0 else p95_ms, latency_sample_count=samples,
            timeout_rate=(timeouts / determined) if determined else None,
            timeout_count=timeouts, timeout_denominator_count=determined, queue_depth=queue,
        )  # fmt: skip

    def push(self, n=1, source=None, flags=(), deliver=True, decide=True, **fields):
        source = source or self.sources[0]
        estimate = None
        for _ in range(n):
            obs = self.telemetry(self.k, source, **fields)
            self.clock.set_time(obs.observation_window_end)
            if deliver:
                self.ingests.append(self.twin.ingest(envelope(obs, flags)))
                self.seq[source] = obs.sequence
            else:
                self.seq[source] += 1  # the window existed but never arrived
            self.k += 1
            if decide:
                estimate = self.decide()
        return estimate

    def snapshot(self):
        return self.twin.snapshot(self.experiment, self.route)

    def decide(self, snapshot=None):
        return self.estimator.update(snapshot or self.snapshot(), self.clock.now())


def envelope(obs, flags=(), trust="AUTHENTICATED"):
    return ValidatedRouteTelemetry(
        schema_version=SCHEMA_VERSION, experiment_id=obs.experiment_id, route_id=obs.route_id,
        telemetry=obs, trust_status=trust, validator_id="validator", validator_version="v1",
        validated_at=obs.observation_timestamp, validation_flags=tuple(flags),
        cross_check_status="INCONSISTENT" if "CROSS_CHECK_DEVIATION" in flags else "UNAVAILABLE",
    )  # fmt: skip


def forged_snapshot(feed, obs):
    """A snapshot built without validation (as a buggy or hostile component could)."""

    good = feed.snapshot()
    return RouteTwinSnapshot.model_construct(**{**dict(good), "latest_trusted_observation": obs})


def warm(feed=None, n=120):
    feed = feed or Feed()
    return feed, feed.push(n, **HEALTHY)


# =============================================================== A. mathematics


def test_ewma_is_time_weighted():
    assert ewma_alpha(30.0, 30.0) == pytest.approx(1 - math.exp(-1))
    assert ewma_alpha(0.0, 30.0) == 0.0 and ewma_alpha(-1.0, 30.0) == 0.0
    assert ewma_update(None, 7.0, 1.0, 30.0) == 7.0
    two_steps = ewma_update(ewma_update(10.0, 20.0, 1.0, 5.0), 20.0, 1.0, 5.0)
    one_step = ewma_update(10.0, 20.0, 2.0, 5.0)
    assert two_steps == pytest.approx(one_step)  # equal elapsed time, equal weight


def test_robust_helpers():
    assert median([]) is None and median([3.0, 1.0, 2.0]) == 2.0 and median([1.0, 3.0]) == 2.0
    assert nearest_rank([5.0, 1.0, 4.0, 2.0, 3.0], 0.2) == 1.0 and nearest_rank([], 0.5) is None
    assert rate_limited(10.0, 50.0, 10.0, 2.0, 0.01) == (10.2, True)
    assert rate_limited(10.0, 4.0, 10.0, 2.0, 0.01) == (4.0, False)  # falls immediately
    assert rate_limited(None, 50.0, 0.0, 1.0, 0.01) == (50.0, False)


def test_zero_samples_initializing_with_no_bound():
    feed = Feed()
    e = feed.decide()
    assert e.mode is EstimatorMode.INITIALIZING
    assert e.capacity_lower_bound_rps is None and e.estimated_capacity_rps is None
    assert e.identifiability is Identifiability.UNIDENTIFIABLE and e.safe_headroom_rps == 0.0
    assert Reason.NO_TRUSTED_OBSERVATION in e.reason_codes


def test_constant_unsaturated_throughput_identifies_only_a_lower_bound():
    """100 rps served of 100 offered is consistent with capacity 100, 500 or 1000."""

    _, e = warm()
    assert e.mode is EstimatorMode.TRUSTED and e.route_state is RouteState.HEALTHY
    assert e.identifiability is Identifiability.LOWER_BOUND_ONLY
    assert e.estimated_capacity_rps is None and e.utilization_estimate is None
    assert e.capacity_lower_bound_rps == pytest.approx(35.0 * LAG)
    assert Reason.CAPACITY_NOT_IDENTIFIABLE in e.reason_codes
    # utilization is only bounded from above: load / lower bound
    assert e.utilization_upper_bound == pytest.approx(e.offered_load_rps / (35.0 * LAG))


def test_capacity_step_down_is_identified_and_old_evidence_dropped():
    feed, before = warm()
    assert before.capacity_lower_bound_rps == pytest.approx(35.0 * LAG)
    e = feed.push(30, **CONGESTED)
    assert e.route_state is RouteState.CONGESTED
    assert e.identifiability is Identifiability.IDENTIFIED
    assert e.estimated_capacity_rps == pytest.approx(20.0)
    assert e.capacity_lower_bound_rps <= 20.0  # the old 35 rps evidence no longer counts
    assert Reason.EVIDENCE_RESET_ON_CONGESTION in e.reason_codes
    assert e.safe_headroom_rps == 0.0


def test_stale_high_throughput_cannot_hide_a_drop():
    feed, _ = warm()
    feed.push(3, **CONGESTED)
    e = feed.push(1, **CONGESTED)
    assert e.capacity_lower_bound_rps is None or e.capacity_lower_bound_rps <= 20.0


def test_capacity_step_up_is_rate_limited():
    cfg = CapacityEstimatorConfig()
    feed, _ = warm(Feed(cfg))
    previous = feed.push(30, **CONGESTED).capacity_lower_bound_rps
    rises = []
    for i in range(40):  # capacity returns: the backlog drains at 50 rps, then load is served
        queue = 300 - 15 * (i + 1)
        e = feed.push(1, payment_rps=35.0, throughput=50.0 if queue > 5 else 35.0, p95_ms=300.0,
                      timeouts=0, queue=max(5, queue))  # fmt: skip
        rises.append(e.capacity_lower_bound_rps - previous)
        ceiling = cfg.max_increase_per_s * max(previous, e.offered_load_rps) * 1.0
        assert e.capacity_lower_bound_rps - previous <= ceiling + 1e-9
        previous = e.capacity_lower_bound_rps
    assert any(r > 0 for r in rises)
    assert Reason.BOUND_RATE_LIMITED in e.reason_codes


def test_recovery_hysteresis_requires_clean_time():
    cfg = CapacityEstimatorConfig()
    feed, _ = warm(Feed(cfg))
    feed.push(30, **CONGESTED)
    states = [feed.push(1, **HEALTHY).route_state for _ in range(40)]
    assert states[0] is RouteState.CONGESTED  # one clean window does not end congestion
    first_recovering = states.index(RouteState.RECOVERING)
    first_healthy = states.index(RouteState.HEALTHY)
    assert first_recovering == cfg.congestion_lookback - 1
    assert first_healthy + 1 >= cfg.recovery_clean_s  # at least 30 s of clean windows
    for s in states[first_recovering:first_healthy]:
        assert s is RouteState.RECOVERING


def test_conservative_lower_bound_resists_a_single_spike():
    feed, base = warm()
    spike = feed.push(1, payment_rps=35.0, throughput=36.0, p95_ms=300.0, timeouts=0, queue=4)
    assert spike.capacity_lower_bound_rps <= base.capacity_lower_bound_rps + 0.1 + 1e-9
    assert max_block_mean([], 10.0) is None


def test_headroom_is_rho_max_bound_minus_load():
    cfg = CapacityEstimatorConfig()
    feed = Feed(cfg)
    feed.push(120, payment_rps=30.0, throughput=30.0, p95_ms=300.0, timeouts=0, queue=4)
    # a healthy period with demonstrated service of 45 rps, then load falls to 20
    feed.push(150, payment_rps=45.0, throughput=45.0, p95_ms=300.0, timeouts=0, queue=6)
    e = feed.push(150, payment_rps=20.0, throughput=20.0, p95_ms=300.0, timeouts=0, queue=3)
    assert e.mode is EstimatorMode.TRUSTED and e.route_state is RouteState.HEALTHY
    expected = cfg.rho_max * e.capacity_lower_bound_rps - e.offered_load_rps
    assert e.safe_headroom_rps == pytest.approx(max(0.0, expected))
    assert e.safe_headroom_rps > 0
    assert e.safe_headroom_rps <= e.capacity_lower_bound_rps - e.offered_load_rps


def _open_headroom(feed=None):
    """Healthy history that demonstrated 45 rps, then load 20: positive headroom."""

    feed = feed or Feed()
    feed.push(120, payment_rps=30.0, throughput=30.0, p95_ms=300.0, timeouts=0, queue=4)
    feed.push(150, payment_rps=45.0, throughput=45.0, p95_ms=300.0, timeouts=0, queue=6)
    e = feed.push(150, payment_rps=20.0, throughput=20.0, p95_ms=300.0, timeouts=0, queue=3)
    assert e.safe_headroom_rps > 0 and e.route_state is RouteState.HEALTHY
    return feed, e


def test_a_single_unconfirmed_congestion_window_vetoes_headroom():
    feed, _ = _open_headroom()
    e = feed.push(1, payment_rps=20.0, throughput=20.0, p95_ms=300.0, timeouts=4, queue=3)
    assert e.route_state is RouteState.HEALTHY  # one window is not a confirmed congestion
    assert e.safe_headroom_rps == 0.0 and Reason.CONGESTION_SIGNAL in e.reason_codes


def test_service_deficit_vetoes_headroom_before_backlog_signals():
    feed, _ = _open_headroom()
    e = feed.push(12, payment_rps=20.0, throughput=17.0, p95_ms=300.0, timeouts=0, queue=4)
    assert e.route_state is RouteState.HEALTHY and Reason.CONGESTION_SIGNAL not in e.reason_codes
    assert e.safe_headroom_rps == 0.0 and Reason.SERVICE_DEFICIT in e.reason_codes


def test_bound_cannot_rise_while_degraded():
    feed, before = warm()
    e = feed.push(60, payment_rps=45.0, throughput=45.0, p95_ms=None, timeouts=0, queue=None,
                  latency_samples=0)  # fmt: skip
    assert e.mode is EstimatorMode.DEGRADED
    assert e.capacity_lower_bound_rps <= before.capacity_lower_bound_rps + 1e-9


def test_no_headroom_when_load_is_at_the_bound():
    _, e = warm()
    assert e.safe_headroom_rps == 0.0 and Reason.NO_HEADROOM_BELOW_LOAD in e.reason_codes


def test_confidence_degrades_with_gaps_and_age():
    feed, healthy = warm()
    feed.push(5, deliver=False, decide=False, **HEALTHY)
    gappy = feed.push(1, **HEALTHY)
    assert gappy.confidence < healthy.confidence
    assert Reason.SEQUENCE_GAP in gappy.reason_codes and gappy.mode is EstimatorMode.DEGRADED
    assert gappy.confidence_kind == "HEURISTIC_UNCALIBRATED"
    feed.clock.advance(4.0)
    older = feed.decide()
    assert older.confidence < gappy.confidence


def test_recovery_after_stale_needs_fresh_evidence_and_bound_stays_frozen():
    feed, before = warm()
    feed.push(20, deliver=False, decide=False, **HEALTHY)  # 20 windows never arrive
    stale = feed.decide()
    assert stale.mode is EstimatorMode.STALE and stale.safe_headroom_rps == 0.0
    assert Reason.STALE_TELEMETRY in stale.reason_codes and stale.confidence == 0.0
    back = feed.push(1, payment_rps=60.0, throughput=60.0, p95_ms=300.0, timeouts=0, queue=5)
    # the first window after the gap cannot raise the bound (gap => degraded => frozen)
    assert back.capacity_lower_bound_rps <= before.capacity_lower_bound_rps + 1e-9
    assert back.mode is EstimatorMode.DEGRADED


# =============================================================== B. input integrity


@pytest.mark.parametrize(
    "field_name, value",
    [
        ("observed_throughput_rps", -5.0),
        ("observed_throughput_rps", float("nan")),
        ("observed_throughput_rps", float("inf")),
        ("payment_rps", -1.0),
        ("p95_latency_ms", float("nan")),
        ("queue_depth", -3),
        ("timeout_rate", 1.5),
    ],
)
def test_malformed_values_fail_closed_without_overwriting_state(field_name, value):
    feed, before = warm()
    good = feed.telemetry(feed.k)
    bad = RouteTelemetry.model_construct(**{**dict(good), field_name: value})
    e = feed.decide(forged_snapshot(feed, bad))
    assert e.mode is EstimatorMode.FAIL_CLOSED and e.safe_headroom_rps == 0.0
    assert Reason.MALFORMED_SAMPLE in e.reason_codes and e.rejected_samples == 1
    after = feed.decide()
    assert after.capacity_lower_bound_rps == before.capacity_lower_bound_rps
    assert after.usable_samples == before.usable_samples


def test_contract_rejects_non_finite_and_negative_before_the_twin():
    feed = Feed()
    with pytest.raises(ValidationError):
        feed.telemetry(0, throughput=float("nan"))
    with pytest.raises(ValidationError):
        feed.telemetry(0, payment_rps=-1.0)


def test_missing_fields_degrade_and_block_headroom():
    feed, _ = warm()
    e = feed.push(30, payment_rps=35.0, throughput=35.0, p95_ms=None, timeouts=0, queue=None,
                  latency_samples=0)  # fmt: skip
    assert e.mode is EstimatorMode.DEGRADED and Reason.MISSING_FIELDS in e.reason_codes
    assert e.safe_headroom_rps == 0.0


def test_duplicate_sequence_changes_nothing():
    feed, before = warm()
    dup = feed.telemetry(feed.k - 1, seq=feed.seq[SOURCE])
    result = feed.twin.ingest(envelope(dup))
    assert result.status in (IngestStatus.DUPLICATE, IngestStatus.CONFLICT)
    e = feed.decide()
    assert e.usable_samples == before.usable_samples
    assert e.capacity_lower_bound_rps == before.capacity_lower_bound_rps


def test_reordered_sample_is_ignored():
    feed, before = warm()
    old = feed.telemetry(5, seq=5)
    e = feed.decide(forged_snapshot(feed, old))
    assert Reason.REORDERED_SAMPLE_IGNORED in e.reason_codes
    assert e.capacity_lower_bound_rps == before.capacity_lower_bound_rps


def test_replayed_old_telemetry_is_rejected_by_the_twin():
    feed, before = warm()
    replay = feed.telemetry(0, seq=0)
    assert not feed.twin.ingest(envelope(replay)).applied
    assert feed.decide().capacity_lower_bound_rps == before.capacity_lower_bound_rps


def test_unauthenticated_sample_never_reaches_the_estimator():
    feed, before = warm()
    forged = feed.telemetry(feed.k, throughput=200.0, payment_rps=200.0)
    result = feed.twin.ingest(envelope(forged, trust="UNAUTHENTICATED"))
    assert result.status is IngestStatus.REJECTED_TRUST
    e = feed.decide()
    assert e.capacity_lower_bound_rps == before.capacity_lower_bound_rps


def test_flagged_high_throughput_cannot_raise_the_bound():
    feed, before = warm()
    feed.push(1, flags=("STATISTICAL_ANOMALY",), payment_rps=200.0, throughput=200.0,
              p95_ms=100.0, timeouts=0, queue=5)  # fmt: skip
    e = feed.decide()
    assert Reason.FLAGGED_TELEMETRY_NEWER in e.reason_codes and e.mode is EstimatorMode.DEGRADED
    assert e.capacity_lower_bound_rps == before.capacity_lower_bound_rps
    assert e.safe_headroom_rps <= before.safe_headroom_rps


def test_conflicting_sources_use_the_minimum_and_degrade():
    feed = Feed(sources=("src-1", "src-2"))
    for _ in range(120):  # each source is self-consistent, but they disagree by 40 %
        feed.push(1, source="src-1", **HEALTHY)
        feed.k -= 1  # the second source reports the same window
        e = feed.push(1, source="src-2", payment_rps=25.0, throughput=25.0, p95_ms=300.0,
                      timeouts=0, queue=5)  # fmt: skip
    assert Reason.SOURCE_CONFLICT in e.reason_codes and e.mode is EstimatorMode.DEGRADED
    assert e.capacity_lower_bound_rps == pytest.approx(25.0 * LAG)  # the conservative source


def test_scope_mismatch_is_ignored():
    feed, before = warm()
    other = feed.telemetry(feed.k, scope="SWITCH_GLOBAL", seq=feed.seq[SOURCE] + 1,
                           throughput=500.0, payment_rps=500.0)  # fmt: skip
    e = feed.decide(forged_snapshot(feed, other))
    assert Reason.SCOPE_MISMATCH_IGNORED in e.reason_codes
    assert e.capacity_lower_bound_rps == before.capacity_lower_bound_rps


@pytest.mark.parametrize("factor", [1.3, 2.0])
def test_sustained_throughput_inflation_trips_conservation(factor):
    feed, _ = warm()
    e = None
    for _ in range(60):
        e = feed.push(1, payment_rps=35.0, throughput=35.0 * factor, p95_ms=300.0, timeouts=0,
                      queue=5)  # fmt: skip
        if e.mode is EstimatorMode.FAIL_CLOSED:
            break
    assert e.mode is EstimatorMode.FAIL_CLOSED
    assert Reason.CONSERVATION_VIOLATION in e.reason_codes
    later = feed.push(30, **HEALTHY)
    assert later.mode is EstimatorMode.FAIL_CLOSED and later.safe_headroom_rps == 0.0


def test_throughput_inconsistent_with_backlog_is_detected():
    feed, _ = warm()
    e = feed.push(10, payment_rps=10.0, throughput=40.0, p95_ms=300.0, timeouts=0, queue=0)
    assert e.mode is EstimatorMode.FAIL_CLOSED


def test_conservation_hold_reinitialises_from_fresh_evidence():
    cfg = CapacityEstimatorConfig(inconsistency_hold_s=60.0)
    feed, _ = warm(Feed(cfg))
    feed.push(30, payment_rps=35.0, throughput=70.0, p95_ms=300.0, timeouts=0, queue=5)
    held = feed.push(10, **HEALTHY)
    assert held.mode is EstimatorMode.FAIL_CLOSED
    e = feed.push(60, **HEALTHY)
    assert e.mode is not EstimatorMode.FAIL_CLOSED and e.usable_samples <= 60


def test_artificially_low_latency_cannot_hide_backlog():
    feed, _ = warm()
    e = feed.push(10, payment_rps=35.0, throughput=20.0, p95_ms=200.0, timeouts=8, queue=300)
    assert e.route_state is RouteState.CONGESTED and e.safe_headroom_rps == 0.0


def test_underreported_timeouts_cannot_hide_backlog():
    feed, _ = warm()
    e = feed.push(10, payment_rps=35.0, throughput=20.0, p95_ms=9000.0, timeouts=0, queue=300)
    assert e.route_state is RouteState.CONGESTED and e.safe_headroom_rps == 0.0


def test_zero_throughput_with_backlog_is_congestion_not_capacity():
    feed, _ = warm()
    e = feed.push(30, payment_rps=35.0, throughput=0.0, p95_ms=None, timeouts=35, queue=500,
                  latency_samples=0)  # fmt: skip
    assert e.route_state is RouteState.CONGESTED and e.safe_headroom_rps == 0.0
    assert e.estimated_capacity_rps is None
    assert e.mode is EstimatorMode.INSUFFICIENT_DATA and Reason.NO_SERVED_WORK in e.reason_codes


def test_empty_route_snapshot_is_initializing():
    feed = Feed()
    snap = feed.snapshot()
    assert snap.latest_trusted_observation is None
    e = feed.decide(snap)
    assert e.mode is EstimatorMode.INITIALIZING and e.safe_headroom_rps == 0.0


def test_future_observation_fails_closed():
    feed, _ = warm()
    future = feed.telemetry(feed.k + 100, seq=feed.seq[SOURCE] + 1)
    e = feed.decide(forged_snapshot(feed, future))
    assert e.mode is EstimatorMode.FAIL_CLOSED and Reason.FUTURE_OBSERVATION in e.reason_codes


def test_identity_mismatch_and_clock_regression_fail_closed():
    feed, _ = warm()
    other = Feed(route="A1-C", clock=feed.clock, twin=feed.twin)
    e = feed.estimator.update(other.snapshot(), feed.clock.now())
    assert e.mode is EstimatorMode.FAIL_CLOSED and Reason.IDENTITY_MISMATCH in e.reason_codes
    assert e.observation_watermark == () and e.route_id == ROUTE  # nothing of A1-C leaks
    e = feed.estimator.update(feed.snapshot(), feed.clock.now() - timedelta(seconds=5))
    assert Reason.CLOCK_REGRESSION in e.reason_codes and e.safe_headroom_rps == 0.0
    with pytest.raises(EstimatorInputError):
        feed.estimator.update(feed.snapshot(), datetime(2026, 4, 1))  # naive datetime
    with pytest.raises(EstimatorInputError):
        feed.estimator.update(feed.snapshot().model_dump(), feed.clock.now())


# =============================================================== E. security integration


def test_explicit_fail_closed_has_zero_headroom_and_release_needs_fresh_evidence():
    feed = Feed()
    feed.push(120, payment_rps=30.0, throughput=30.0, p95_ms=300.0, timeouts=0, queue=4)
    feed.push(150, payment_rps=45.0, throughput=45.0, p95_ms=300.0, timeouts=0, queue=6)
    open_ = feed.push(150, payment_rps=20.0, throughput=20.0, p95_ms=300.0, timeouts=0, queue=3)
    assert open_.safe_headroom_rps > 0
    feed.estimator.latch_fail_closed()
    closed = feed.decide()
    assert closed.mode is EstimatorMode.FAIL_CLOSED and closed.safe_headroom_rps == 0.0
    assert Reason.EXPLICIT_FAIL_CLOSED in closed.reason_codes
    feed.estimator.release_fail_closed()
    after = feed.decide()
    assert after.mode is EstimatorMode.INITIALIZING and after.safe_headroom_rps == 0.0


def test_missing_telemetry_reduces_confidence_and_blocks_headroom():
    _, complete = warm()
    feed = Feed()
    for _ in range(12):  # one window in fifteen never arrives
        feed.push(14, **HEALTHY)
        feed.push(1, deliver=False, decide=False, **HEALTHY)
    e = feed.push(1, **HEALTHY)
    assert e.mode is EstimatorMode.DEGRADED and Reason.SEQUENCE_GAP in e.reason_codes
    assert e.confidence < complete.confidence
    assert e.safe_headroom_rps == 0.0 and Reason.CONSISTENCY_UNVERIFIED in e.reason_codes


def test_headroom_requires_conclusive_consistency_after_timeouts():
    feed = Feed()
    feed.push(120, payment_rps=30.0, throughput=30.0, p95_ms=300.0, timeouts=0, queue=4)
    feed.push(150, payment_rps=45.0, throughput=45.0, p95_ms=300.0, timeouts=0, queue=6)
    # timeouts below the congestion threshold rate but enough to loosen the check
    e = feed.push(60, payment_rps=20.0, throughput=20.0, p95_ms=300.0, timeouts=1, queue=3)
    assert e.route_state is RouteState.HEALTHY
    assert e.safe_headroom_rps == 0.0 and Reason.CONSISTENCY_UNVERIFIED in e.reason_codes


# =============================================================== C. system boundaries


def _interleave(feeds, n):
    out = {id(f): [] for f in feeds}
    for _ in range(n):
        for f in feeds:
            out[id(f)].append(f.push(1, **HEALTHY).digest())
    return [out[id(f)] for f in feeds]


def test_no_cross_route_or_cross_experiment_leakage():
    clock = ManualClock(T0)
    a = Feed(clock=clock)
    b = Feed(route="A1-C", clock=clock, twin=a.twin)
    c = Feed(experiment="EXP-OTHER", clock=clock, twin=a.twin)
    together = _interleave([a, b, c], 40)
    for i, route, exp in ((0, ROUTE, EXP), (1, "A1-C", EXP), (2, ROUTE, "EXP-OTHER")):
        alone = Feed(route=route, experiment=exp)
        assert [alone.push(1, **HEALTHY).digest() for _ in range(40)] == together[i]


def test_deterministic_replay_and_stable_serialization():
    first = [Feed().push(1 + i, **HEALTHY) for i in range(3)][-1]
    second = [Feed().push(1 + i, **HEALTHY) for i in range(3)][-1]
    assert first.digest() == second.digest()
    assert CapacityEstimate.model_validate_json(first.model_dump_json()) == first


def test_cross_process_digest_is_stable():
    code = (
        "from tests.intelligence.test_capacity_estimator import Feed, HEALTHY\n"
        "print(Feed().push(150, **HEALTHY).digest())\n"
    )
    out = subprocess.run(  # noqa: S603 - fixed interpreter and constant code
        [sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        env={"PYTHONHASHSEED": "11", "PATH": "/usr/bin:/bin"},
    ).stdout.strip()  # fmt: skip
    assert out == Feed().push(150, **HEALTHY).digest()


def test_bounded_memory_and_computation():
    cfg = CapacityEstimatorConfig(history_limit_windows=200)
    feed = Feed(cfg)
    began = time.perf_counter()
    feed.push(1500, **HEALTHY)
    elapsed = time.perf_counter() - began
    assert all(len(s.windows) <= 200 for s in feed.estimator._sources.values())
    assert elapsed / 1500 < 0.02  # generous: about 0.5 ms per update in practice


def _imports(path):
    tree = ast.parse(path.read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names, tree


def test_estimator_cannot_reach_simulation_truth_or_backend():
    names, tree = _imports(REPO_ROOT / "intelligence/ml/capacity_estimator.py")
    forbidden = ("intelligence.simulation", "intelligence.data", "intelligence.experiments",
                 "intelligence.twin.transaction_twin", "backend", "simpy")  # fmt: skip
    assert not [n for n in names if n.startswith(forbidden)], names
    identifiers = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    identifiers |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not identifiers & {"ground_truth", "TickMetrics", "DesInterval", "capacity_rps",
                              "credited", "intervals"}  # fmt: skip


def test_evaluation_never_imports_backend_or_mutates_ledgers():
    names, _ = _imports(REPO_ROOT / "intelligence/experiments/capacity_evaluation.py")
    assert not [n for n in names if n.startswith("backend")]
    assert "intelligence.twin.transaction_twin" not in names


def test_annotate_snapshot_uses_existing_contract_fields_without_mutation():
    feed, e = warm()
    snap = feed.snapshot()
    annotated = annotate_snapshot(snap, e)
    assert snap.estimator_id is None and snap.capacity_lower_bound_rps is None
    assert annotated.estimator_id == "aorts-capacity"
    assert annotated.capacity_lower_bound_rps == e.capacity_lower_bound_rps
    assert annotated.telemetry_confidence == e.confidence
    other = Feed(route="A1-C")
    with pytest.raises(EstimatorInputError):
        annotate_snapshot(other.snapshot(), e)


def test_estimate_model_enforces_safety_invariants():
    _, e = warm()
    base = e.model_dump()
    with pytest.raises(ValidationError):
        CapacityEstimate(**{**base, "mode": "FAIL_CLOSED", "safe_headroom_rps": 1.0})
    with pytest.raises(ValidationError):
        CapacityEstimate(**{**base, "safe_headroom_rps": 1e6})
    with pytest.raises(ValidationError):
        CapacityEstimate(**{**base, "safe_headroom_rps": float("nan")})
    with pytest.raises(ValidationError):
        CapacityEstimate(**{**base, "estimated_capacity_rps": 1.0,
                            "identifiability": "IDENTIFIED"})  # fmt: skip
    with pytest.raises(ValidationError):
        CapacityEstimate(**{**base, "reason_codes": ("WARMING_UP", "STALE_TELEMETRY")})


def test_config_validation():
    with pytest.raises(ValidationError):
        CapacityEstimatorConfig(congestion_confirm=5, congestion_lookback=3)
    with pytest.raises(ValidationError):
        CapacityEstimatorConfig(point_horizon_s=900.0, evidence_horizon_s=600.0)
    with pytest.raises(ValidationError):
        CapacityEstimatorConfig(rho_max=1.5)
    with pytest.raises(ValidationError):
        CapacityEstimatorConfig(lag_allowance_s=10.0, sustain_s=10.0)
    with pytest.raises(ValidationError):
        CapacityEstimatorConfig(unknown=1)
    with pytest.raises(EstimatorInputError):
        CapacityEstimator(EXP, ROUTE, config={"rho_max": 0.5})
    assert CapacityEstimatorConfig().digest() == CapacityEstimatorConfig().digest()
