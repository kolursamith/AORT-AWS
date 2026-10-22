"""Stage 04: fluid (Stage 3B) versus discrete-event (Stage 04) comparison methodology.

Known disagreements are recorded as strict expected failures with their measured cause, so a
future Stage 3B fix turns them into visible XPASS failures instead of silently changing results.
"""

import math

import pytest

pytest.importorskip("simpy")  # declared in pyproject's ``research`` extra (simpy==4.1.1)

from intelligence.data.generate_dataset import generate_workload  # noqa: E402
from intelligence.simulation import model_validation as mv  # noqa: E402
from intelligence.simulation.des_validate import CheckRelease  # noqa: E402


def case(name: str) -> mv.ValidationCase:
    every = (
        *mv.DEVELOPMENT_CASES,
        *mv.HOLDOUT_CASES,
        *mv.FRESH_HOLDOUT_CASES,
        *mv.HOLDOUT_V2_CASES,
    )
    return next(c for c in every if c.name == name)


# ================================================================== error metrics


def test_metrics_are_exact_on_known_series():
    a, b = [1.0, 2.0, 4.0], [1.0, 1.0, 1.0]
    assert mv.mae(a, b) == pytest.approx(4 / 3)
    assert mv.rmse(a, b) == pytest.approx(math.sqrt(10 / 3))
    assert mv.max_abs_error(a, b) == 3.0
    assert mv.mae([], []) == 0.0 and mv.rmse([], []) == 0.0
    with pytest.raises(ValueError, match="lengths differ"):
        mv.mae([1.0], [])


def test_relative_error_is_zero_safe():
    assert mv.relative_error(3, 0) == 3.0  # floor 1 when the reference is 0
    assert mv.relative_error(110, 100) == pytest.approx(0.10)
    assert mv.relative_error(0, 0) == 0.0
    assert mv.relative_error(2, 0, floor=4.0) == 0.5
    with pytest.raises(ValueError):
        mv.relative_error(1, 1, floor=0.0)


def test_percentile_uses_nearest_rank():
    assert mv.percentile([], 0.5) is None
    assert mv.percentile([3.0, 1.0, 2.0, 4.0], 0.5) == 2.0
    assert mv.percentile([3.0, 1.0, 2.0, 4.0], 0.95) == 4.0


def test_recovery_time_needs_a_full_hold_below_the_threshold():
    hold = int(mv.RECOVERY_HOLD_S)
    delays = [5.0] * 10 + [1.0] * (hold - 1) + [3.0] + [1.0] * hold
    assert mv.recovery_time(delays, after_s=5.0) == 10.0 + hold - 5.0
    assert mv.recovery_time([1.0] * hold, after_s=0.0) == 0.0
    assert mv.recovery_time([1.0] * (hold - 1), after_s=0.0) is None
    assert mv.recovery_time([None] * (2 * hold), after_s=0.0) is None
    exact = [mv.RECOVERY_DELAY_THRESHOLD_S] * hold
    assert mv.recovery_time(exact, after_s=0.0) is None  # strictly below the threshold


def test_capacity_series_averages_half_open_intervals():
    workload = generate_workload(case("short_fault").build())  # cut at 100 s for 30 s
    series = mv.capacity_series(workload, "A1-B", 200)
    assert series[99] == 10.0 and series[100] == pytest.approx(6.5)
    assert series[129] == pytest.approx(6.5) and series[130] == 10.0


def test_fluid_driver_requires_timestep_aligned_releases():
    workload = generate_workload(case("no_load").build())
    with pytest.raises(ValueError, match="timestep boundaries"):
        mv.run_fluid(workload, [CheckRelease(10.5, "A1-B", 1)])


# =========================================================== criteria and evidence


def test_criteria_are_declared_and_versioned():
    assert mv.CRITERIA_VERSION == "stage04-criteria/1"
    assert {c.name for c in mv.CRITERIA} == {
        "conservation",
        "served_total",
        "queue_mae",
        "queue_peak",
        "new_ambiguous_total",
        "recovery_time",
        "unresolved_at_end",
        "check_results",
        "latency_quantiles",
        "classification_agreement",
    }
    assert all(c.rule and c.rationale for c in mv.CRITERIA)


@pytest.fixture(scope="module")
def stable():
    return mv.run_case(case("stable_under_capacity"))


def test_matched_runs_share_arrivals_and_conserve_work(stable):
    fluid = [m.payment_arrivals for m in stable.fluid.result.metrics]
    des = [i.payment_arrivals for i in stable.des.intervals]
    assert fluid == des
    assert stable.comparison.conservation_fluid == 0
    assert set(stable.comparison.conservation_des.values()) == {0}
    assert stable.comparison.classification_agreement == 1.0
    evaluated = {o.criterion for o in stable.outcomes}
    assert evaluated == {c.name for c in mv.CRITERIA}


AGREEING = (
    "no_load",
    "stable_under_capacity",
    "near_saturation",
    "overload",
    "short_fault",
    "long_fault",
    "status_check_bursts",
    "delayed_and_lost_responses",
    "recovery_and_draining",
    "shared_bottleneck",
)


@pytest.mark.parametrize("name", AGREEING)
def test_development_cases_meet_the_declared_criteria(name):
    run = mv.run_case(case(name))
    assert run.passed, [o for o in run.outcomes if not o.passed]


# ============================== (B) historical negative evidence under criteria/1
# These record how the fluid model FAILED stage04-criteria/1. They are deterministic
# reproductions of preserved results, not acceptance tests and not hidden expected failures.


def _criteria1_failures(name, dt=1.0):
    run = mv.run_case(case(name), dt_s=dt)
    return run, sorted({o.criterion for o in run.outcomes if not o.passed})


def test_historical_criteria1_customer_repayment_dt1_failed():
    run, failing = _criteria1_failures("customer_repayment")
    assert failing == ["new_ambiguous_total", "queue_mae", "recovery_time", "unresolved_at_end"]
    route = run.comparison.routes[0]
    assert (route.new_ambiguous_fluid, route.new_ambiguous_des) == (4352, 3871)


def test_customer_repayment_meets_the_criteria_at_a_quarter_second():
    run = mv.run_case(case("customer_repayment"), dt_s=0.25)
    assert run.passed, [o for o in run.outcomes if not o.passed]


def test_fluid_uses_full_capacity_in_a_stale_drop_regime():
    """Was a strict xfail before the Stage 04R repair of ``_drop_stale``."""

    run = mv.run_case(case("long_fault"))
    rows = run.fluid.result.metrics
    drop_ticks = [m.tick for m in rows if m.dropped]
    window = range(min(drop_ticks), max(drop_ticks) + 1)
    work = sum(rows[t].capacity_rps for t in window)
    assert sum(rows[t].served for t in window) >= math.floor(work) - 1


def test_des_uses_full_capacity_in_the_same_regime():
    run = mv.run_case(case("long_fault"))
    rows = run.fluid.result.metrics
    drop_ticks = [m.tick for m in rows if m.dropped]
    window = range(min(drop_ticks), max(drop_ticks) + 1)
    work = sum(rows[t].capacity_rps for t in window)
    served = sum(run.des.intervals[t].served for t in window)
    assert math.floor(work) - 1 <= served <= math.ceil(work) + 1


# ======================================================================== holdout


@pytest.mark.parametrize("name", ["holdout_S1", "holdout_S3"])
def test_holdout_cases_without_stale_drops_agree(name):
    run = mv.run_case(case(name))
    assert run.passed, [o for o in run.outcomes if not o.passed]


def test_historical_criteria1_holdout_s2_dt1_failed():
    run, failing = _criteria1_failures("holdout_S2")
    assert failing == ["recovery_time", "unresolved_at_end"]
    route = run.comparison.routes[0]
    assert (route.unresolved_end_fluid, route.unresolved_end_des) == (1229, 837)


def test_holdout_s2_converges_with_the_timestep():
    errors = {}
    for dt in (1.0, 0.25, 0.1):
        route = mv.run_case(case("holdout_S2"), dt_s=dt).comparison.routes[0]
        errors[dt] = abs(route.unresolved_end_fluid - route.unresolved_end_des)
    assert errors[0.1] < errors[0.25] < errors[1.0]


def test_repair_removes_resolution_divergence_in_a_drop_regime():
    """Pre-repair, the fluid backlog error grew as dt shrank; now it shrinks."""

    gaps = []
    for dt in (1.0, 0.25, 0.1):
        route = mv.run_case(case("long_fault"), dt_s=dt).comparison.routes[0]
        gaps.append(abs(route.unresolved_end_fluid - route.unresolved_end_des))
    assert gaps[-1] <= gaps[0] <= 5


def test_recovery_sensitivity_is_labelled_and_keeps_the_official_metric():
    route = mv.run_case(case("holdout_S2")).comparison.routes[0]
    thresholds = [t for t, _, _ in route.recovery_sensitivity]
    assert thresholds == list(mv.RECOVERY_SENSITIVITY_THRESHOLDS_S)
    official = dict((t, (f, d)) for t, f, d in route.recovery_sensitivity)[2.0]
    assert official == (route.recovery_fluid_s, route.recovery_des_s) == (293.0, 421.0)
    assert dict((t, (f, d)) for t, f, d in route.recovery_sensitivity)[2.5] == (292.0, 292.0)


# ============================================================ fresh holdout (Stage 04R)


def test_fresh_holdout_declaration_is_frozen():
    assert mv.FRESH_HOLDOUT_SEEDS == (1001, 1002, 1003, 1004, 1005)
    assert mv.FRESH_HOLDOUT_TIMESTEPS_S == (1.0, 0.25, 0.1)
    assert len(mv.FRESH_HOLDOUT_CASES) == 15
    assert mv.CRITERIA_VERSION == "stage04-criteria/1"


def test_fresh_holdout_s3_agrees_at_a_quarter_second():
    run = mv.run_case(case("fresh_S3_seed1001"), dt_s=0.25)
    assert run.passed, [o for o in run.outcomes if not o.passed]


def test_historical_criteria1_fresh_s2_seed1004_dt01_failed():
    run, failing = _criteria1_failures("fresh_S2_seed1004", dt=0.1)
    assert failing == ["check_results"]
    route = run.comparison.routes[0]
    assert (route.check_results_fluid, route.check_results_des) == (562, 494)


def test_criteria1_is_unchanged():
    assert mv.CRITERIA_VERSION == "stage04-criteria/1"
    rule = next(c.rule for c in mv.CRITERIA if c.name == "latency_quantiles")
    assert rule.startswith("|p50_des - p50_fluid| <= 1.5 * dt")


# ======================================================= (C) criteria/2 machinery


def test_criteria2_is_versioned_and_keeps_the_unchanged_measures():
    assert mv.CRITERIA_V2_VERSION == "stage04-criteria/2"
    v1 = {c.name: c.rule for c in mv.CRITERIA}
    v2 = {c.name: c.rule for c in mv.CRITERIA_V2}
    assert "latency_quantiles" not in v2
    for name in ("conservation", "served_total", "queue_mae", "queue_peak", "new_ambiguous_total",
                 "recovery_time", "unresolved_at_end", "check_results",
                 "classification_agreement"):  # fmt: skip
        assert v2[name] == v1[name]
    assert {"latency_quantiles_v2", "check_flow_conservation", "released_alignment",
            "answer_rate_alignment"} <= set(v2)  # fmt: skip
    assert (mv.LATENCY_RELATIVE_TOLERANCE, mv.ANSWER_RATE_TOLERANCE) == (0.05, 0.05)


@pytest.fixture(scope="module")
def bursts():
    return mv.run_case(case("status_check_bursts"))


def _flows(run):
    timeout = run.workload.manifest.config.timing_policy.check_timeout_s
    return (
        mv.check_flow_fluid(run.fluid.result, run.case.schedule, timeout),
        mv.check_flow_des(run.des, run.case.schedule, timeout),
    )


def _with_latency(comparison, p50f, p50d, p95f, p95d):
    from dataclasses import replace

    return replace(comparison, latency_p50_fluid_s=p50f, latency_p50_des_s=p50d,
                   latency_p95_fluid_s=p95f, latency_p95_des_s=p95d,
                   latency_samples=(100, 100))  # fmt: skip


def _latency(outcomes):
    return next(o for o in outcomes if o.criterion == "latency_quantiles_v2")


def test_latency_tolerance_is_quantisation_plus_relative_reference_term(bursts):
    ff, fd = _flows(bursts)
    base = bursts.comparison
    # p95 = 12 s at dt = 0.1: limit 0.15 + 0.6 = 0.75 s
    ok = mv.evaluate_v2(_with_latency(base, 0.40, 0.40, 12.74, 12.0), ff, fd, dt=0.1)
    bad = mv.evaluate_v2(_with_latency(base, 0.40, 0.40, 12.76, 12.0), ff, fd, dt=0.1)
    assert _latency(ok).passed and not _latency(bad).passed
    # low latency is NOT relaxed: p50 = 0.4 s allows 0.15 + 0.02 = 0.17 s only
    low = mv.evaluate_v2(_with_latency(base, 0.58, 0.40, 1.0, 1.0), ff, fd, dt=0.1)
    assert not _latency(low).passed


def test_check_flow_is_conserved_in_both_models(bursts):
    ff, fd = _flows(bursts)
    assert ff.conserved and fd.conserved
    assert ff.proposed == fd.proposed == sum(r.count for r in bursts.case.schedule)
    assert ff.released == ff.enqueued and fd.released == fd.enqueued


def test_check_flow_detects_an_accounting_hole(bursts):
    from dataclasses import replace

    ff, _ = _flows(bursts)
    assert not replace(ff, answered=ff.answered + 1).conserved
    assert not replace(ff, shortfall=ff.shortfall - 1).conserved


def test_pool_exhaustion_is_identified(bursts):
    ff, fd = _flows(bursts)
    assert ff.shortfall > 0 and fd.shortfall > 0
    assert ff.pool_limited_releases > 0 and fd.pool_limited_releases > 0


def _outcome(budget, objective, amb=0, unres=0, queue=0.0):
    return mv.BudgetOutcome(budget, objective, amb, unres, 0, queue)


def test_budget_ranking_agreement_and_material_reversal_are_detected():
    same = [_outcome(b, j) for b, j in zip(mv.BUDGET_CANDIDATES, (10, 8, 9, 20), strict=True)]
    d = mv.budget_diagnostic(same, same)
    assert d.ranking_agrees and d.regret == 0 and not d.material_reversal
    fluid = [_outcome(b, j) for b, j in zip(mv.BUDGET_CANDIDATES, (10, 30, 30, 5), strict=True)]
    des = [_outcome(b, j) for b, j in zip(mv.BUDGET_CANDIDATES, (100, 90, 95, 200), strict=True)]
    d = mv.budget_diagnostic(fluid, des)
    assert (d.best_fluid, d.best_des, d.regret, d.material_reversal) == (50, 5, 110, True)
    minor = [_outcome(b, j) for b, j in zip(mv.BUDGET_CANDIDATES, (100, 92, 95, 200), strict=True)]
    close = [_outcome(b, j) for b, j in zip(mv.BUDGET_CANDIDATES, (100, 95, 92, 200), strict=True)]
    d = mv.budget_diagnostic(close, minor)
    assert d.best_fluid != d.best_des and not d.material_reversal and not d.ranking_agrees


def test_budget_direction_disagreement_is_detected():
    up = [_outcome(0, 1, amb=100, queue=5.0), _outcome(5, 1), _outcome(20, 1),
          _outcome(50, 1, amb=150, queue=9.0)]  # fmt: skip
    down = [_outcome(0, 1, amb=100, queue=5.0), _outcome(5, 1), _outcome(20, 1),
            _outcome(50, 1, amb=60, queue=9.0)]  # fmt: skip
    d = mv.budget_diagnostic(up, down)
    assert d.sign_disagreements == ("new_ambiguous: fluid +1, des -1",)


def _passing(case_name="x"):
    return [mv.CriterionOutcome(case_name, None, c.name, True, "") for c in mv.CRITERIA_V2]


def test_classification_rules(bursts):
    comparison = bursts.comparison
    agree = mv.budget_diagnostic(*[[_outcome(b, 1.0) for b in mv.BUDGET_CANDIDATES]] * 2)
    assert mv.classify_run(comparison, _passing(), agree)[0] is mv.ModelClass.GREEN
    assert mv.classify_run(comparison, _passing(), None)[0] is mv.ModelClass.AMBER
    failing = _passing()
    failing[0] = mv.CriterionOutcome("x", None, "queue_mae", False, "")
    assert mv.classify_run(comparison, failing, agree)[0] is mv.ModelClass.AMBER
    broken = _passing()
    broken[0] = mv.CriterionOutcome("x", None, "check_flow_conservation", False, "")
    assert mv.classify_run(comparison, broken, agree)[0] is mv.ModelClass.RED
    fluid = [_outcome(b, j) for b, j in zip(mv.BUDGET_CANDIDATES, (10, 30, 30, 5), strict=True)]
    des = [_outcome(b, j) for b, j in zip(mv.BUDGET_CANDIDATES, (100, 90, 95, 200), strict=True)]
    reversal = mv.budget_diagnostic(fluid, des)
    cls, reasons = mv.classify_run(comparison, _passing(), reversal)
    assert cls is mv.ModelClass.RED and "material budget reversal" in reasons[0]
    assert mv.worst([mv.ModelClass.GREEN, mv.ModelClass.RED, mv.ModelClass.AMBER]) is (
        mv.ModelClass.RED
    )


def test_holdout_v2_declaration_is_frozen():
    assert mv.HOLDOUT_V2_SEEDS == tuple(range(2001, 2011))
    assert mv.HOLDOUT_V2_TIMESTEPS_S == (1.0, 0.25, 0.1)
    assert len(mv.HOLDOUT_V2_CASES) == 30
    assert mv.BUDGET_CANDIDATES == (0, 5, 20, 50)
    used = set(mv.FRESH_HOLDOUT_SEEDS) | {1, 2, 3, 5, 6, 11, 12, 1000}
    assert used.isdisjoint(mv.HOLDOUT_V2_SEEDS)
    declaration = mv.holdout_v2_declaration()
    assert declaration["criteria"] == "stage04-criteria/2"


# ================================== (C) criteria/2 acceptance against the frozen holdout


def test_holdout_v2_timeout_regime_is_green_at_a_quarter_second():
    record = mv.run_v2(case("v2_S3_seed2001"), 0.25)
    assert record.model_class is mv.ModelClass.GREEN, record.reasons
    assert all(not failing for _, failing in record.failing)
    assert all(ff.conserved and fd.conserved for _, ff, fd in record.flows)


def test_holdout_v2_stale_regime_is_red_at_one_second():
    record = mv.run_v2(case("v2_S2_seed2001"), 1.0)
    assert record.model_class is mv.ModelClass.RED
    assert "A1-B: unresolved error > 2x tolerance" in record.reasons
    assert not record.diagnostic.material_reversal
    assert all(ff.conserved and fd.conserved for _, ff, fd in record.flows)
