"""Stage 07: feedback-aware counterfactual look-ahead (pure model, no simulator).

Numbered comments refer to the Stage 07 acceptance list.
"""

import ast
import math
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from contracts.v1_1 import CheckResultKind
from intelligence.controller.lookahead import (
    Band,
    CheckCandidate,
    CheckEvidence,
    CheckOutcomeCounts,
    FeedbackMode,
    ForecastInput,
    ForecastInputError,
    ForecastReason,
    LookaheadConfig,
    benefit_lower_bound,
    candidate_benefit,
    choose_budget,
    forecast_budgets,
)
from intelligence.ml.capacity_estimator import (
    CapacityEstimate,
    EstimatorMode,
    Identifiability,
    Reason,
    RouteState,
)
from intelligence.simulation.scenarios import research_timing_policy

REPO = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
EXP, ROUTE = "EXP-LA", "A1-B"
POLICY = research_timing_policy()
MANY_OUTCOMES = CheckOutcomeCounts(authoritative=40, unknown=5, pending=5)


def estimate(**kw) -> CapacityEstimate:
    base = dict(
        config_digest="c" * 64,
        experiment_id=EXP,
        route_id=ROUTE,
        decision_time=T0,
        snapshot_version=10,
        observation_watermark=(),
        last_observation_at=T0,
        observation_age_s=0.0,
        twin_freshness="FRESH",
        mode=EstimatorMode.TRUSTED,
        route_state=RouteState.HEALTHY,
        identifiability=Identifiability.IDENTIFIED,
        observed_offered_rps=14.0,
        observed_throughput_rps=14.0,
        offered_load_rps=14.0,
        estimated_capacity_rps=20.0,
        capacity_lower_bound_rps=19.0,
        utilization_estimate=0.7,
        utilization_upper_bound=14.0 / 19.0,
        safe_headroom_rps=0.0,
        confidence=0.9,
        usable_samples=100,
        saturation_samples=5,
        rejected_samples=0,
        reason_codes=(),
    )
    return CapacityEstimate(**{**base, **kw})


def candidates(n, *, route=ROUTE, exp=EXP, eligible_offset_s=-1.0, **kw):
    return tuple(
        CheckCandidate(
            experiment_id=exp,
            route_id=route,
            transaction_id=f"T{i:05d}",
            version=2,
            eligible_at=T0 + timedelta(seconds=eligible_offset_s),
            checks_used=0,
            deadline_at=None,
            **kw,
        )
        for i in range(n)
    )


def data(**kw) -> ForecastInput:
    base = dict(
        experiment_id=EXP,
        route_id=ROUTE,
        decision_time=T0,
        capacity=estimate(),
        observed_outstanding=4,
        current_check_rps=0.0,
        ambiguous_backlog=500,
        late_resolution_hazard_per_s=0.001,
        candidates=candidates(500),
        check_outcomes=MANY_OUTCOMES,
        observed_median_latency_s=0.35,
        observed_timeout_rate=0.0,
    )
    return ForecastInput(**{**base, **kw})


def central(result, rate):
    return result.budget(rate).band(Band.CENTRAL)


# ---------------------------------------------------------------- 1, 2, 15, 16


def test_01_determinism():
    a = forecast_budgets(data(), [1, 3, 8], POLICY)
    b = forecast_budgets(data(), [8, 3, 1], POLICY)
    assert a.digest() == b.digest() and a == b
    assert [x.budget_rps for x in a.budgets] == [0, 1, 3, 8]  # zero is always evaluated


def test_02_zero_budget_releases_nothing_and_a_healthy_route_stays_calm():
    r = forecast_budgets(data(), [0], POLICY)
    z = central(r, 0)
    assert z.checks_released == 0 and z.expected_authoritative == 0
    assert z.queue_mean < 1.0 and z.new_ambiguous < 0.01
    assert r.budget(0).incremental_ambiguity == 0.0


def test_15_units_rates_counts_and_milliseconds():
    cfg = LookaheadConfig(horizon_s=30.0)
    r = forecast_budgets(data(), [2], POLICY, cfg)
    c = central(r, 2)
    assert r.horizon_s == 30.0 and r.steps == 30
    assert c.checks_released == pytest.approx(2 * 30)  # checks/s x s
    assert c.payment_arrivals == pytest.approx(14.0 * 30)  # requests/s x s
    assert c.predicted_payment_delay_ms == pytest.approx(
        (c.mean_payment_wait_s + cfg.latency_floor_s) * 1000.0
    )


def test_16_check_work_is_counted_exactly_once():
    r = forecast_budgets(data(current_check_rps=2.0), [5], POLICY)
    assert r.exogenous_payment_rps == pytest.approx(12.0)  # own checks removed from offered load
    for band in r.budget(5).bands:
        total_in = band.queue_start + band.payment_arrivals + band.repay_arrivals
        total_in += band.checks_released
        assert total_in == pytest.approx(band.served + band.dropped + band.queue_end)
        assert abs(band.conservation_residual) < 1e-6
        assert band.checks_on_time <= band.checks_released + 1e-9
        assert band.expected_authoritative <= band.checks_on_time + 1e-9


# ---------------------------------------------------------------- 3, 4, 5


def test_03_positive_checks_add_queue_load():
    tight = data(capacity=estimate(estimated_capacity_rps=16.0, capacity_lower_bound_rps=15.5))
    r = forecast_budgets(tight, [1, 4, 8], POLICY)
    q = [central(r, b).queue_mean for b in (0, 1, 4, 8)]
    assert q == sorted(q) and q[-1] > q[0] + 10
    assert central(r, 8).checks_released > central(r, 4).checks_released


def test_04_saturation_queues_by_age_and_times_out():
    hot = data(
        capacity=estimate(
            estimated_capacity_rps=10.0,
            capacity_lower_bound_rps=9.5,
            route_state=RouteState.CONGESTED,
        ),
        observed_median_latency_s=20.0,
    )
    cfg = LookaheadConfig(horizon_s=120.0, feedback_mode=FeedbackMode.NO_REPAY)
    r = forecast_budgets(hot, [0], POLICY, cfg)
    z = central(r, 0)
    assert ForecastReason.SATURATED in r.reasons
    assert z.dropped > 0 and z.mean_payment_wait_s > POLICY.client_timeout_s
    # stale drops act on age: the queue holds about arrival rate x drop age, not capacity x age
    assert z.queue_end == pytest.approx(14.0 * 30.0, rel=0.1)
    assert z.new_ambiguous > 0.9 * z.payment_arrivals


def test_05_recovery_checks_induce_new_ambiguity_monotonically():
    near = data(capacity=estimate(estimated_capacity_rps=15.0, capacity_lower_bound_rps=14.5))
    r = forecast_budgets(near, [2, 5, 10, 20], POLICY, LookaheadConfig(horizon_s=120.0))
    incr = [r.budget(b).incremental_ambiguity for b in (0, 2, 5, 10, 20)]
    assert incr[0] == 0.0 and incr[-1] > 10.0
    assert all(b >= a - 1e-9 for a, b in zip(incr, incr[1:], strict=False))
    assert central(r, 20).induced_check_demand > central(r, 0).induced_check_demand
    assert central(r, 20).repay_arrivals > central(r, 0).repay_arrivals


# ---------------------------------------------------------------- 6, 7 ablations


def test_06_no_feedback_removes_only_the_feedback_mechanism():
    near = data(capacity=estimate(estimated_capacity_rps=15.0, capacity_lower_bound_rps=14.5))
    cfg = LookaheadConfig(horizon_s=120.0)
    full = forecast_budgets(near, [20], POLICY, cfg)
    off = forecast_budgets(
        near, [20], POLICY, cfg.model_copy(update={"feedback_mode": FeedbackMode.NO_FEEDBACK})
    )
    assert full.budget(20).incremental_ambiguity > 10.0
    assert abs(off.budget(20).incremental_ambiguity) < 1e-9
    f, o = central(full, 20), central(off, 20)
    assert o.checks_released == f.checks_released  # eligibility and limits untouched
    assert o.checks_on_time == pytest.approx(f.checks_on_time)  # checks still see the queue
    # the only extra queue in FULL is re-payment load created by check-induced ambiguity
    assert f.queue_mean > o.queue_mean and o.repay_arrivals < 1e-3 < f.repay_arrivals
    no_repay = cfg.model_copy(update={"repay_probability": 0.0})
    f0 = central(forecast_budgets(near, [20], POLICY, no_repay), 20)
    o0 = central(
        forecast_budgets(
            near,
            [20],
            POLICY,
            no_repay.model_copy(update={"feedback_mode": FeedbackMode.NO_FEEDBACK}),
        ),
        20,
    )
    assert o0.queue_mean == pytest.approx(f0.queue_mean) == pytest.approx(o.queue_mean)
    assert off.feedback_mode is FeedbackMode.NO_FEEDBACK and off.available


def test_07_no_repay_removes_only_repayment_pressure():
    hot = data(
        capacity=estimate(estimated_capacity_rps=13.0, capacity_lower_bound_rps=12.5),
        observed_median_latency_s=10.0,
    )
    cfg = LookaheadConfig(horizon_s=120.0)
    full = forecast_budgets(hot, [3], POLICY, cfg)
    off = forecast_budgets(
        hot, [3], POLICY, cfg.model_copy(update={"feedback_mode": FeedbackMode.NO_REPAY})
    )
    assert central(full, 0).repay_arrivals > 0 and central(off, 0).repay_arrivals == 0
    assert central(full, 0).new_ambiguous >= central(off, 0).new_ambiguous
    assert central(off, 3).checks_released == central(full, 3).checks_released


# ---------------------------------------------------------------- 8 information boundary


def test_08_no_hidden_truth_imports():
    tree = ast.parse((REPO / "intelligence/controller/lookahead.py").read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules |= {a.name for a in node.names}
    forbidden = (
        "intelligence.simulation",
        "intelligence.data",
        "intelligence.experiments",
        "intelligence.twin.transaction_twin",
        "backend",
        "simpy",
    )
    assert not [m for m in modules if m.startswith(forbidden)], modules
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not names & {
        "ground_truth",
        "credited",
        "response_lost",
        "capacity_rps_truth",
        "TickMetrics",
        "DesInterval",
    }


# ---------------------------------------------------------------- 9, 10, 18 trust


@pytest.mark.parametrize(
    "mode, reason",
    [
        (EstimatorMode.STALE, ForecastReason.CAPACITY_STALE),
        (EstimatorMode.FAIL_CLOSED, ForecastReason.ESTIMATOR_FAIL_CLOSED),
        (EstimatorMode.INITIALIZING, ForecastReason.ESTIMATOR_INITIALIZING),
    ],
)
def test_09_unusable_capacity_gives_no_forecast(mode, reason):
    cap = estimate(
        mode=mode,
        estimated_capacity_rps=None,
        capacity_lower_bound_rps=None,
        identifiability=Identifiability.UNIDENTIFIABLE,
        utilization_estimate=None,
        utilization_upper_bound=None,
    )
    r = forecast_budgets(data(capacity=cap), [1, 5], POLICY)
    assert not r.available and r.budgets == () and reason in r.reasons


def test_09c_insufficient_data_with_a_zero_bound_is_unavailable():
    cap = estimate(
        mode=EstimatorMode.INSUFFICIENT_DATA,
        estimated_capacity_rps=None,
        capacity_lower_bound_rps=0.0,
        identifiability=Identifiability.UNIDENTIFIABLE,
        utilization_estimate=None,
        utilization_upper_bound=None,
    )
    r = forecast_budgets(data(capacity=cap), [1], POLICY)
    assert not r.available and ForecastReason.CAPACITY_UNAVAILABLE in r.reasons


def test_09b_missing_bound_is_unavailable():
    cap = estimate(
        mode=EstimatorMode.INSUFFICIENT_DATA,
        estimated_capacity_rps=None,
        capacity_lower_bound_rps=None,
        identifiability=Identifiability.UNIDENTIFIABLE,
        utilization_estimate=None,
        utilization_upper_bound=None,
    )
    r = forecast_budgets(data(capacity=cap), [1], POLICY)
    assert not r.available and ForecastReason.CAPACITY_UNAVAILABLE in r.reasons


def test_10_unknown_check_benefit_is_conservative():
    cfg = LookaheadConfig()
    p, identified = benefit_lower_bound(CheckOutcomeCounts(authoritative=3), cfg)
    assert (p, identified) == (cfg.prior_low, False)
    p_many, ok = benefit_lower_bound(MANY_OUTCOMES, cfg)
    assert ok and 0 < p_many < (40 + 1) / (50 + 5)  # a lower bound, below the posterior mean
    r = forecast_budgets(data(check_outcomes=CheckOutcomeCounts()), [3], POLICY)
    assert ForecastReason.CHECK_BENEFIT_UNIDENTIFIED in r.reasons
    assert r.check_benefit_low == cfg.prior_low and not r.check_benefit_identified
    unknown = candidates(1, evidence=CheckEvidence.LAST_CHECK_UNKNOWN)[0]
    fresh = candidates(1)[0]
    assert candidate_benefit(unknown, 0.8, cfg) == pytest.approx(0.8 * cfg.unknown_factor)
    congested = candidates(1, sent_while_congested=True)[0]
    assert candidate_benefit(congested, 0.8, cfg) == pytest.approx(0.8 * cfg.congested_factor)
    assert candidate_benefit(fresh, 0.8, cfg) == 0.8


def test_10b_outcome_counts_record_each_kind():
    c = CheckOutcomeCounts()
    for kind in (
        CheckResultKind.AUTHORITATIVE_SUCCESS,
        CheckResultKind.AUTHORITATIVE_FAILURE,
        CheckResultKind.UNKNOWN,
        CheckResultKind.PENDING,
        None,
    ):
        c = c.record(kind)
    assert (c.authoritative, c.unknown, c.pending, c.timed_out, c.total) == (2, 1, 1, 1, 5)


def test_18_source_trust_is_propagated():
    degraded = estimate(mode=EstimatorMode.DEGRADED, reason_codes=(Reason.FLAGGED_TELEMETRY_NEWER,))
    r = forecast_budgets(data(capacity=degraded), [1], POLICY)
    assert r.available
    assert {ForecastReason.TRUST_DEGRADED, ForecastReason.FLAGGED_TELEMETRY} <= set(r.reasons)


# ---------------------------------------------------------------- 11, 12, 13 validation


def test_11_12_route_and_experiment_isolation():
    with pytest.raises(ForecastInputError):
        forecast_budgets(data(candidates=candidates(2, route="A1-C")), [1], POLICY)
    with pytest.raises(ForecastInputError):
        forecast_budgets(data(candidates=candidates(2, exp="EXP-OTHER")), [1], POLICY)
    with pytest.raises(ForecastInputError):
        forecast_budgets(data(capacity=estimate(route_id="A1-C")), [1], POLICY)
    with pytest.raises(ForecastInputError):
        forecast_budgets(data(capacity=estimate(experiment_id="EXP-OTHER")), [1], POLICY)
    dup = candidates(2)
    with pytest.raises(ForecastInputError):
        forecast_budgets(data(candidates=(dup[0], dup[0])), [1], POLICY)


@pytest.mark.parametrize(
    "field_name, value",
    [
        ("late_resolution_hazard_per_s", float("nan")),
        ("late_resolution_hazard_per_s", -0.1),
        ("ambiguous_backlog", -1),
        ("observed_outstanding", float("inf")),
        ("current_check_rps", -2.0),
        ("observed_median_latency_s", float("nan")),
        ("observed_timeout_rate", 1.5),
    ],
)
def test_13_negative_and_nonfinite_inputs_are_rejected(field_name, value):
    with pytest.raises(ForecastInputError):
        forecast_budgets(data(**{field_name: value}), [1], POLICY)


@pytest.mark.parametrize("budgets", [[-1], [True], [1.5]])
def test_13b_budgets_must_be_nonnegative_integers(budgets):
    with pytest.raises(ForecastInputError):
        forecast_budgets(data(), budgets, POLICY)


def test_13c_future_capacity_and_naive_time_are_rejected():
    with pytest.raises(ForecastInputError):
        forecast_budgets(
            data(capacity=estimate(decision_time=T0 + timedelta(seconds=5))), [1], POLICY
        )
    with pytest.raises(ForecastInputError):
        forecast_budgets(data(decision_time=datetime(2026, 5, 1, 12)), [1], POLICY)


# ---------------------------------------------------------------- 14, 17, 19


def test_14_runtime_is_bounded_and_long_horizons_are_truncated():
    cfg = LookaheadConfig(horizon_s=100_000.0, max_steps=600)
    began = time.perf_counter()
    r = forecast_budgets(data(), list(range(0, 40, 2)), POLICY, cfg)
    elapsed = time.perf_counter() - began
    assert r.steps == 600 and ForecastReason.HORIZON_TRUNCATED in r.reasons
    assert elapsed < 10.0  # 20 budgets x 3 bands x 600 steps; measured well under 1 s


def test_17_band_disagreement_is_detected():
    # capacity unidentified: the pessimistic band (load) saturates, the optimistic one does not
    wide = data(
        capacity=estimate(
            identifiability=Identifiability.LOWER_BOUND_ONLY,
            estimated_capacity_rps=None,
            capacity_lower_bound_rps=14.0,
            utilization_estimate=None,
        )
    )
    r = forecast_budgets(wide, [2, 4, 8], POLICY, LookaheadConfig(horizon_s=120.0))
    assert ForecastReason.BAND_DISAGREEMENT in r.reasons
    assert ForecastReason.CAPACITY_UNIDENTIFIED in r.reasons
    pess, opt = central(r, 8), r.budget(8).band(Band.OPTIMISTIC)
    assert r.budget(8).band(Band.PESSIMISTIC).queue_mean > pess.queue_mean >= opt.queue_mean


def test_19_no_eligible_checks_makes_every_budget_equal_to_zero():
    r = forecast_budgets(data(candidates=()), [1, 5], POLICY)
    assert ForecastReason.NO_ELIGIBLE_CHECKS in r.reasons
    zero = central(r, 0)
    for b in (1, 5):
        assert central(r, b) == zero and r.budget(b).incremental_ambiguity == 0


def test_19b_checks_wait_for_eligibility():
    later = candidates(10, eligible_offset_s=20.0)
    r = forecast_budgets(data(candidates=later), [5], POLICY, LookaheadConfig(horizon_s=30.0))
    assert r.eligible_now == 0 and r.eligible_in_horizon == 10
    assert central(r, 5).checks_released == 10  # released after t0 + 20 s, each once
    slow = forecast_budgets(
        data(candidates=candidates(10, eligible_offset_s=25.0)),
        [1],
        POLICY,
        LookaheadConfig(horizon_s=30.0),
    )
    assert central(slow, 1).checks_released == 5  # only steps 25..29 may release one each
    beyond = candidates(10, eligible_offset_s=100.0)
    r2 = forecast_budgets(data(candidates=beyond), [5], POLICY, LookaheadConfig(horizon_s=30.0))
    assert central(r2, 5).checks_released == 0


# ---------------------------------------------------------------- observable-model details


def test_latency_evidence_overrides_a_congestion_label_caused_by_lost_confirmations():
    lossy = estimate(
        route_state=RouteState.CONGESTED,
        identifiability=Identifiability.LOWER_BOUND_ONLY,
        estimated_capacity_rps=None,
        capacity_lower_bound_rps=10.0,
        utilization_estimate=None,
    )
    healthy_latency = data(
        capacity=lossy, observed_median_latency_s=0.35, observed_timeout_rate=0.3
    )
    r = forecast_budgets(healthy_latency, [0], POLICY)
    assert ForecastReason.LATENCY_SHOWS_NO_QUEUE in r.reasons
    assert ForecastReason.NONQUEUE_TIMEOUTS in r.reasons
    z = central(r, 0)
    assert z.queue_mean < 1.0  # capacity at least the served load: no phantom queue
    assert z.new_ambiguous == pytest.approx(0.3 * z.payment_arrivals, rel=0.05)
    slow = data(capacity=lossy, observed_median_latency_s=8.0, observed_timeout_rate=0.3)
    r2 = forecast_budgets(slow, [0], POLICY)
    assert ForecastReason.NONQUEUE_TIMEOUTS not in r2.reasons  # unidentifiable when queueing


def test_queue_falls_back_to_outstanding_without_latency():
    r = forecast_budgets(data(observed_median_latency_s=None, observed_outstanding=50), [0], POLICY)
    assert ForecastReason.QUEUE_FROM_OUTSTANDING in r.reasons
    assert central(r, 0).queue_start == pytest.approx(50 - 14.0 * 0.3)


def test_deadline_misses_count_due_candidates():
    due = tuple(
        CheckCandidate(
            EXP, ROUTE, f"D{i}", 2, T0 - timedelta(seconds=1), 0, T0 + timedelta(seconds=10)
        )
        for i in range(4)
    )
    r = forecast_budgets(data(candidates=due), [2], POLICY, LookaheadConfig(horizon_s=30.0))
    assert central(r, 0).deadline_misses == pytest.approx(4.0)
    assert central(r, 2).deadline_misses < 4.0


def test_legacy_choose_budget_still_works():
    assert choose_budget(10.0, 7.0, [0.0, 1.0, 2.0, 5.0]) == 2.0
    assert math.isclose(choose_budget(1.0, 7.0, [0.0, 1.0]), 0.0)
