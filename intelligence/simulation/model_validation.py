"""Fluid (Stage 3B) versus discrete-event (Stage 04) model comparison.

Both models consume the same Stage 3A workload (identical arrivals, faults, capacity, beneficiary
truth and pre-drawn customer intent) and the same policy-neutral status-check schedule. They are
then compared on aggregate behaviour, never byte for byte: the fluid model completes work in
whole timesteps, the discrete-event model at exact instants, so individual completion times
legitimately differ by up to one timestep.

Agreement criteria (``CRITERIA``) were written into this module **before** the first holdout
(S1/S2/S3) comparison was executed. Changing a threshold after seeing holdout results makes the
result exploratory and must be recorded as such; ``CRITERIA_VERSION`` identifies the set used.
Ground truth is read here only for evaluation (capacity series, recovery definition).
"""

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from intelligence.data.generate_dataset import Workload, generate_workload
from intelligence.simulation.des_validate import (
    CheckRelease,
    DesConfig,
    DesResult,
    ServiceDistribution,
    conservation_residuals,
    simulate_des,
)
from intelligence.simulation.engine import (
    CheckDecision,
    FluidSimulation,
    SimulationConfig,
    SimulationResult,
)
from intelligence.simulation.scenarios import (
    BeneficiarySpec,
    CheckDemandSpec,
    FaultSpec,
    RepaySpec,
    RouteSpec,
    ScenarioConfig,
    ScenarioOptions,
    reference_scenario,
    research_timing_policy,
)

CRITERIA_VERSION = "stage04-criteria/1"
RECOVERY_DELAY_THRESHOLD_S = 2.0  # proposal: switch delay below 2 s ...
RECOVERY_HOLD_S = 60.0  # ... for 60 s
# Sensitivity analysis only (Stage 04R); never replaces the official 2 s metric.
RECOVERY_SENSITIVITY_THRESHOLDS_S = (2.0, 2.5, 3.0)


# ------------------------------------------------------------------------ error metrics


def absolute_error(value: float, reference: float) -> float:
    return abs(value - reference)


def relative_error(value: float, reference: float, floor: float = 1.0) -> float:
    """``|value - reference| / max(|reference|, floor)``: defined when the reference is 0."""

    if floor <= 0:
        raise ValueError("floor must be positive")
    return abs(value - reference) / max(abs(reference), floor)


def mae(series: Sequence[float], reference: Sequence[float]) -> float:
    _same_length(series, reference)
    if not series:
        return 0.0
    return sum(abs(a - b) for a, b in zip(series, reference, strict=True)) / len(series)


def rmse(series: Sequence[float], reference: Sequence[float]) -> float:
    _same_length(series, reference)
    if not series:
        return 0.0
    return math.sqrt(
        sum((a - b) ** 2 for a, b in zip(series, reference, strict=True)) / len(series)
    )


def max_abs_error(series: Sequence[float], reference: Sequence[float]) -> float:
    _same_length(series, reference)
    return max((abs(a - b) for a, b in zip(series, reference, strict=True)), default=0.0)


def _same_length(a: Sequence[float], b: Sequence[float]) -> None:
    if len(a) != len(b):
        raise ValueError(f"series lengths differ: {len(a)} != {len(b)}")


def percentile(values: Sequence[float], fraction: float) -> float | None:
    """Nearest-rank percentile; None for an empty sample."""

    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


# ---------------------------------------------------------------- matched model runs


@dataclass(frozen=True)
class FluidRun:
    result: SimulationResult
    response_s: dict[str, float]  # original payments answered within the client timeout
    ambiguous: frozenset[str]  # original payments that became ambiguous


def run_fluid(
    workload: Workload,
    schedule: Sequence[CheckRelease] = (),
    *,
    enforce_timing_policy: bool = True,
    bottleneck_groups: tuple[tuple[str, ...], ...] = (),
    dt_s: float = 1.0,
) -> FluidRun:
    """Run the fluid engine with the external schedule translated to per-tick decisions."""

    config = SimulationConfig(
        dt_s=dt_s,
        enforce_timing_policy=enforce_timing_policy,
        bottleneck_groups=bottleneck_groups,
    )
    by_tick: dict[int, dict[str, int]] = {}
    for release in schedule:
        tick = round(release.time_s / config.dt_s)
        if not math.isclose(tick * config.dt_s, release.time_s, abs_tol=1e-9):
            raise ValueError("fluid comparison needs release times on timestep boundaries")
        by_tick.setdefault(tick, {})
        by_tick[tick][release.route_id] = by_tick[tick].get(release.route_id, 0) + release.count
    sim = FluidSimulation(workload, config)
    epoch = workload.manifest.config.epoch
    originals = {p.transaction_id for p in workload.exogenous.payments}
    responses: dict[str, float] = {}
    ambiguous: set[str] = set()
    while not sim.done:
        decisions = [
            CheckDecision(route_id=route, count=count)
            for route, count in sorted(by_tick.get(sim.tick, {}).items())
        ]
        observation = sim.step(decisions)
        for outcome in observation.outcomes:
            if outcome.how == "TIMELY_RESPONSE" and outcome.transaction_id in originals:
                responses[outcome.transaction_id] = (outcome.observed_at - epoch).total_seconds()
        ambiguous.update(t for t in observation.newly_ambiguous if t in originals)
    return FluidRun(sim.result(), responses, frozenset(ambiguous))


# --------------------------------------------------------------------- per-route series


def capacity_series(workload: Workload, route: str, bins: int, bin_s: float = 1.0) -> list[float]:
    """Evaluation only: time-weighted true capacity per bin."""

    changes = sorted(
        (c.offset_s, c.capacity_rps) for c in workload.ground_truth.capacity if c.route_id == route
    )
    out = []
    for k in range(bins):
        start, end = k * bin_s, (k + 1) * bin_s
        total, current, cursor = 0.0, 0.0, start
        for offset, value in changes:
            if offset <= start:
                current = value
                continue
            if offset >= end:
                break
            total += current * (offset - cursor)
            cursor, current = offset, value
        total += current * (end - cursor)
        out.append(total / bin_s)
    return out


def fault_end(workload: Workload, route: str) -> float | None:
    ends = [f.end_offset_s for f in workload.ground_truth.faults
            if f.route_id == route and f.kind == "CAPACITY_CUT"]  # fmt: skip
    return max(ends) if ends else None


def recovery_time(
    delays: Sequence[float | None],
    after_s: float,
    bin_s: float = 1.0,
    threshold_s: float | None = None,
) -> float | None:
    """Seconds from ``after_s`` until estimated delay stays below the threshold for 60 s.

    The official metric uses ``RECOVERY_DELAY_THRESHOLD_S`` (2 s). Other thresholds are for
    labelled sensitivity analysis only. Returns None when the condition is never met.
    """

    threshold = RECOVERY_DELAY_THRESHOLD_S if threshold_s is None else threshold_s
    hold = round(RECOVERY_HOLD_S / bin_s)
    first = math.ceil(after_s / bin_s - 1e-9)
    run = 0
    for k in range(first, len(delays)):
        d = delays[k]
        run = run + 1 if d is not None and d < threshold else 0
        if run == hold:
            return (k - hold + 1) * bin_s - after_s
    return None


@dataclass(frozen=True)
class RouteComparison:
    route_id: str
    capacity_max_rps: float
    queue_mae: float
    queue_rmse: float
    queue_max_abs: float
    queue_mean_fluid: float
    peak_fluid: int
    peak_des: int
    served_fluid: int
    served_des: int
    new_ambiguous_fluid: int
    new_ambiguous_des: int
    check_results_fluid: int
    check_results_des: int
    wasted_fluid: int
    wasted_des: int
    unresolved_end_fluid: int
    unresolved_end_des: int
    recovery_fluid_s: float | None
    recovery_des_s: float | None
    served_series_mae: float
    cumulative_ambiguous_mae: float
    # Labelled sensitivity analysis: (threshold_s, fluid recovery, DES recovery).
    recovery_sensitivity: tuple[tuple[float, float | None, float | None], ...] = ()


@dataclass(frozen=True)
class CaseComparison:
    case: str
    routes: tuple[RouteComparison, ...]
    conservation_fluid: int
    conservation_des: dict[str, int]
    latency_p50_fluid_s: float | None
    latency_p50_des_s: float | None
    latency_p95_fluid_s: float | None
    latency_p95_des_s: float | None
    latency_samples: tuple[int, int]
    classification_agreement: float | None
    classified_payments: int
    timeout_deviation: int  # DES - fluid new ambiguous, all routes


def _fluid_conservation(result: SimulationResult) -> int:
    classes = ("payment", "repayment", "resubmission", "status_check", "background_check")
    still = sum(
        result.total(f"arrived_{c}") - result.total(f"completed_{c}") - result.total(f"dropped_{c}")
        for c in classes
    )
    return still - result.total("queued_at_end")


def compare(case: str, workload: Workload, fluid: FluidRun, des: DesResult) -> CaseComparison:
    routes = tuple(r.route_id for r in workload.manifest.config.routes)
    bins = des.bins
    fm = fluid.result.metrics
    if len(fm) != bins * len(routes):
        raise ValueError("fluid and DES runs cover different horizons")
    comparisons = []
    bin_s = des.config.bin_s
    for route in routes:
        f_rows = [m for m in fm if m.route_id == route]
        d_rows = [i for i in des.intervals if i.route_id == route]
        capacity = capacity_series(workload, route, bins, des.config.bin_s)
        fq = [m.queue_depth for m in f_rows]
        dq = [i.queue_depth for i in d_rows]
        group_capacity = [m.capacity_rps for m in f_rows]
        f_delay = [m.est_queue_delay_s for m in f_rows]
        members = _group_of(fluid, route)
        group_cap = [0.0] * bins
        for x in fm:
            if x.route_id in members:
                group_cap[x.tick] += x.capacity_rps
        d_delay = [
            row.bottleneck_queue_depth / cap if cap > 0 else None
            for row, cap in zip(d_rows, group_cap, strict=True)
        ]
        del group_capacity
        end = fault_end(workload, route)
        f_cum, d_cum, acc_f, acc_d = [], [], 0, 0
        for frow, drow in zip(f_rows, d_rows, strict=True):
            acc_f += frow.new_ambiguous
            acc_d += drow.new_ambiguous
            f_cum.append(acc_f)
            d_cum.append(acc_d)
        comparisons.append(
            RouteComparison(
                route_id=route,
                capacity_max_rps=max(capacity, default=0.0),
                queue_mae=mae(dq, fq),
                queue_rmse=rmse(dq, fq),
                queue_max_abs=max_abs_error(dq, fq),
                queue_mean_fluid=sum(fq) / len(fq) if fq else 0.0,
                peak_fluid=max(fq, default=0),
                peak_des=max(dq, default=0),
                served_fluid=sum(m.served for m in f_rows),
                served_des=sum(i.served for i in d_rows),
                new_ambiguous_fluid=sum(m.new_ambiguous for m in f_rows),
                new_ambiguous_des=sum(i.new_ambiguous for i in d_rows),
                check_results_fluid=sum(m.check_results for m in f_rows),
                check_results_des=sum(i.check_results for i in d_rows),
                wasted_fluid=sum(m.wasted_checks for m in f_rows),
                wasted_des=sum(i.wasted_checks for i in d_rows),
                unresolved_end_fluid=f_rows[-1].outstanding_ambiguous if f_rows else 0,
                unresolved_end_des=len(dict(des.observations[-1].ambiguous_outstanding)[route]),
                recovery_fluid_s=None if end is None else recovery_time(f_delay, end, bin_s),
                recovery_des_s=None if end is None else recovery_time(d_delay, end, bin_s),
                recovery_sensitivity=()
                if end is None
                else tuple(
                    (
                        t,
                        recovery_time(f_delay, end, bin_s, t),
                        recovery_time(d_delay, end, bin_s, t),
                    )
                    for t in RECOVERY_SENSITIVITY_THRESHOLDS_S
                ),
                served_series_mae=mae([i.served for i in d_rows], [m.served for m in f_rows]),
                cumulative_ambiguous_mae=mae(d_cum, f_cum),
            )
        )

    sent = {p.transaction_id: p.offset_s for p in workload.exogenous.payments}
    f_lat = [fluid.response_s[t] - sent[t] for t in fluid.response_s]
    d_traces = {t.txn: t for t in des.traces if t.kind == "payment"}
    timeout = workload.manifest.config.timing_policy.client_timeout_s
    d_timely = {
        t.txn: t.response_s - t.sent_s
        for t in d_traces.values()
        if t.response_s is not None and t.response_s <= t.sent_s + timeout
    }
    d_lat = list(d_timely.values())
    horizon = des.horizon_s
    decided = [t for t in sent if sent[t] + timeout < horizon]

    def label(timely: bool, ambiguous: bool) -> str:
        return "timely" if timely else ("ambiguous" if ambiguous else "undecided")

    agree = sum(
        label(t in fluid.response_s, t in fluid.ambiguous)
        == label(t in d_timely, d_traces[t].ambiguous_s is not None)
        for t in decided
    )
    return CaseComparison(
        case=case,
        routes=tuple(comparisons),
        conservation_fluid=_fluid_conservation(fluid.result),
        conservation_des=conservation_residuals(des),
        latency_p50_fluid_s=percentile(f_lat, 0.50),
        latency_p50_des_s=percentile(d_lat, 0.50),
        latency_p95_fluid_s=percentile(f_lat, 0.95),
        latency_p95_des_s=percentile(d_lat, 0.95),
        latency_samples=(len(f_lat), len(d_lat)),
        classification_agreement=agree / len(decided) if decided else None,
        classified_payments=len(decided),
        timeout_deviation=sum(r.new_ambiguous_des - r.new_ambiguous_fluid for r in comparisons),
    )


def _group_of(fluid: FluidRun, route: str) -> tuple[str, ...]:
    for group in fluid.result.config.bottleneck_groups:
        if route in group:
            return group
    return (route,)


# --------------------------------------------------------------- agreement criteria


@dataclass(frozen=True)
class Criterion:
    name: str
    rule: str
    rationale: str


DT_S = 1.0  # timestep of the primary (stage04-criteria/1) comparison

CRITERIA: tuple[Criterion, ...] = (
    Criterion(
        "conservation",
        "arrived - completed - dropped - queued == 0 in both models, per class",
        "Work conservation is exact in both models; any residual is a defect.",
    ),
    Criterion(
        "served_total",
        "|served_des - served_fluid| <= max(0.02 * served_fluid, c_max * dt + 2)",
        "Requests in progress at the horizon differ by at most one timestep of service "
        "(c_max * dt); 2 % absorbs stale-drop boundary differences.",
    ),
    Criterion(
        "queue_mae",
        "MAE(queue_des, queue_fluid) <= max(c_max * dt + 1, 0.10 * mean(queue_fluid))",
        "The fluid model finishes requests at timestep ends, the DES at exact instants: "
        "snapshots differ by up to one timestep of service plus the request in service.",
    ),
    Criterion(
        "queue_peak",
        "|peak_des - peak_fluid| <= max(c_max * dt + 2, 0.10 * peak_fluid)",
        "Same resolution argument as queue_mae, applied to the maximum.",
    ),
    Criterion(
        "new_ambiguous_total",
        "|amb_des - amb_fluid| <= max(2 * c_max * dt + 5, 0.10 * amb_fluid)",
        "A payment times out when its delay crosses the 15 s client timeout; the fluid "
        "model's completion times are quantised to dt, which shifts the crossing by up to "
        "dt for about c_max * dt payments at each crossing (onset and recovery).",
    ),
    Criterion(
        "recovery_time",
        "both None, or both defined and |rec_des - rec_fluid| <= 10 s",
        "Recovery uses a 2 s delay threshold held for 60 s on 1 s samples; queue snapshots "
        "near the threshold can shift the first qualifying sample by several seconds.",
    ),
    Criterion(
        "unresolved_at_end",
        "|unres_des - unres_fluid| <= max(c_max * dt + 2, 0.10 * unres_fluid)",
        "End-of-run backlog differs by in-flight work at the horizon.",
    ),
    Criterion(
        "check_results",
        "|checks_des - checks_fluid| <= max(3, 0.10 * checks_fluid)",
        "Check selection depends on which payments are ambiguous, which itself carries the "
        "new_ambiguous tolerance.",
    ),
    Criterion(
        "latency_quantiles",
        "|p50_des - p50_fluid| <= 1.5 * dt and |p95_des - p95_fluid| <= 1.5 * dt "
        "(when both have >= 20 timely payments)",
        "Fluid completions are delayed to the timestep end (up to dt) relative to exact "
        "service; the DES has no such quantisation.",
    ),
    Criterion(
        "classification_agreement",
        "share of original payments with the same timely/ambiguous/undecided label >= 0.95 "
        "(when >= 20 payments are decided)",
        "Only payments near the timeout boundary may flip between models.",
    ),
)


@dataclass(frozen=True)
class CriterionOutcome:
    case: str
    route_id: str | None
    criterion: str
    passed: bool
    detail: str


def evaluate(comparison: CaseComparison, dt: float = DT_S) -> tuple[CriterionOutcome, ...]:
    out: list[CriterionOutcome] = []
    case = comparison.case

    def add(route, name, passed, detail):
        out.append(CriterionOutcome(case, route, name, bool(passed), detail))

    des_ok = all(v == 0 for v in comparison.conservation_des.values())
    add(None, "conservation", comparison.conservation_fluid == 0 and des_ok,
        f"fluid {comparison.conservation_fluid}, des {comparison.conservation_des}")  # fmt: skip
    for r in comparison.routes:
        c = r.capacity_max_rps
        limit = max(0.02 * r.served_fluid, c * dt + 2)
        add(r.route_id, "served_total", abs(r.served_des - r.served_fluid) <= limit,
            f"{r.served_des} vs {r.served_fluid} (limit {limit:.1f})")  # fmt: skip
        limit = max(c * dt + 1, 0.10 * r.queue_mean_fluid)
        add(r.route_id, "queue_mae", r.queue_mae <= limit,
            f"MAE {r.queue_mae:.2f} (limit {limit:.1f})")  # fmt: skip
        limit = max(c * dt + 2, 0.10 * r.peak_fluid)
        add(r.route_id, "queue_peak", abs(r.peak_des - r.peak_fluid) <= limit,
            f"{r.peak_des} vs {r.peak_fluid} (limit {limit:.1f})")  # fmt: skip
        limit = max(2 * c * dt + 5, 0.10 * r.new_ambiguous_fluid)
        add(r.route_id, "new_ambiguous_total",
            abs(r.new_ambiguous_des - r.new_ambiguous_fluid) <= limit,
            f"{r.new_ambiguous_des} vs {r.new_ambiguous_fluid} (limit {limit:.1f})")  # fmt: skip
        both_none = r.recovery_fluid_s is None and r.recovery_des_s is None
        both = r.recovery_fluid_s is not None and r.recovery_des_s is not None
        add(r.route_id, "recovery_time",
            both_none or (both and abs(r.recovery_des_s - r.recovery_fluid_s) <= 10.0),
            f"{r.recovery_des_s} vs {r.recovery_fluid_s}")  # fmt: skip
        limit = max(c * dt + 2, 0.10 * r.unresolved_end_fluid)
        add(r.route_id, "unresolved_at_end",
            abs(r.unresolved_end_des - r.unresolved_end_fluid) <= limit,
            f"{r.unresolved_end_des} vs {r.unresolved_end_fluid} (limit {limit:.1f})")  # fmt: skip
        limit = max(3, 0.10 * r.check_results_fluid)
        add(r.route_id, "check_results",
            abs(r.check_results_des - r.check_results_fluid) <= limit,
            f"{r.check_results_des} vs {r.check_results_fluid} (limit {limit:.1f})")  # fmt: skip
    if min(comparison.latency_samples) >= 20:
        d50 = abs(comparison.latency_p50_des_s - comparison.latency_p50_fluid_s)
        d95 = abs(comparison.latency_p95_des_s - comparison.latency_p95_fluid_s)
        add(None, "latency_quantiles", d50 <= 1.5 * dt and d95 <= 1.5 * dt,
            f"|dp50| {d50:.3f} s, |dp95| {d95:.3f} s")  # fmt: skip
    if comparison.classified_payments >= 20:
        share = comparison.classification_agreement
        add(None, "classification_agreement", share >= 0.95,
            f"{share:.4f} of {comparison.classified_payments}")  # fmt: skip
    return tuple(out)


# ------------------------------------------------------------------------------ cases


def _route(route_id="A1-B", payment_rps=7.0, capacity_rps=10.0, remitter="A1", beneficiary="B"):
    return RouteSpec(
        route_id=route_id,
        remitter_bank_id=remitter,
        beneficiary_bank_id=beneficiary,
        payment_rps=payment_rps,
        capacity_rps=capacity_rps,
    )


def _cut(start, end, magnitude=0.35, route="A1-B", kind="CAPACITY_CUT"):
    return FaultSpec(fault_id=f"{kind.lower()}-{route}-{int(start)}", route_id=route,
                     kind=kind, start_s=start, end_s=end, magnitude=magnitude)  # fmt: skip


def _scenario(case, routes, faults=(), repay=0.0, duration=600.0, seed=5, **extra):
    return ScenarioConfig(
        scenario_id=f"des-{case}",
        description=f"Stage 04 validation case {case}",
        reference="Stage 04 development set",
        experiment_id="EXP-DES",
        seed=seed,
        duration_s=duration,
        routes=routes,
        faults=faults,
        repay=RepaySpec(probability=repay),
        beneficiary=BeneficiarySpec(base_rejection_prob=0.0),
        timing_policy=research_timing_policy(),
        **extra,
    )


def periodic_schedule(routes, start, end, every, count) -> tuple[CheckRelease, ...]:
    return tuple(
        CheckRelease(float(t), route, count)
        for t in range(int(start), int(end), int(every))
        for route in routes
    )


@dataclass(frozen=True)
class ValidationCase:
    name: str
    build: Callable[[], ScenarioConfig]
    schedule: tuple[CheckRelease, ...] = ()
    bottleneck_groups: tuple[tuple[str, ...], ...] = ()
    holdout: bool = False


DEVELOPMENT_CASES: tuple[ValidationCase, ...] = (
    ValidationCase(
        "no_load", lambda: _scenario("no_load", (_route(payment_rps=0.0),), duration=300)
    ),
    ValidationCase(
        "stable_under_capacity", lambda: _scenario("stable", (_route(payment_rps=5.0),))
    ),
    ValidationCase("near_saturation", lambda: _scenario("near_sat", (_route(payment_rps=9.5),))),
    ValidationCase(
        "overload", lambda: _scenario("overload", (_route(payment_rps=13.0),), duration=300)
    ),
    ValidationCase("short_fault", lambda: _scenario("short_fault", (_route(),), (_cut(100, 130),))),
    ValidationCase(
        "long_fault", lambda: _scenario("long_fault", (_route(),), (_cut(100, 700),), duration=1200)
    ),
    ValidationCase(
        "status_check_bursts",
        lambda: _scenario("bursts", (_route(),), (_cut(100, 200, 1.0, kind="RESPONSE_LOSS"),)),
        schedule=periodic_schedule(("A1-B",), 190, 400, 10, 25),
    ),
    ValidationCase(
        "delayed_and_lost_responses",
        lambda: _scenario(
            "delayed_lost",
            (_route(),),
            (
                _cut(100, 300, 0.5, kind="RESPONSE_LOSS"),
                _cut(100, 300, 20.0, kind="CONFIRMATION_DELAY"),
            ),  # fmt: skip
        ),
        schedule=periodic_schedule(("A1-B",), 190, 500, 10, 10),
    ),
    ValidationCase(
        "customer_repayment",
        lambda: _scenario("repay", (_route(),), (_cut(100, 400),), repay=1.0, duration=900),
    ),
    ValidationCase(
        "recovery_and_draining",
        lambda: _scenario("drain", (_route(),), (_cut(100, 250, 0.6),), duration=900),
    ),
    ValidationCase(
        "shared_bottleneck",
        lambda: _scenario(
            "shared",
            (
                _route(payment_rps=4.0, capacity_rps=5.0),
                _route("A2-C", 4.0, 5.0, "A2", "C"),
            ),  # fmt: skip
            (_cut(100, 400, 0.5),),
            duration=900,
            check_demand=(
                CheckDemandSpec(
                    demand_id="bg",
                    bank_id="A3",
                    route_id="A2-C",
                    start_s=0.0,
                    end_s=900.0,
                    rate_rps=0.3,
                ),
            ),  # fmt: skip
        ),
        bottleneck_groups=(("A1-B", "A2-C"),),
    ),
)


def _holdout(name: str) -> ScenarioConfig:
    return reference_scenario(
        name,
        seed=11,
        experiment_id="EXP-DES-HOLDOUT",
        rate_scale=0.01,
        duration_s=2400.0,
        options=ScenarioOptions(),
    )


HOLDOUT_CASES: tuple[ValidationCase, ...] = tuple(
    ValidationCase(
        f"holdout_{name}",
        (lambda n=name: _holdout(n)),
        schedule=periodic_schedule(("A1-B",), 100, 2400, 10, 20),
        holdout=True,
    )
    for name in ("S1", "S2", "S3")
)


# ------------------------------------------------------------- Stage 04R fresh holdout
# DECLARED 2026-10-22, before any run of these seeds (Stage 04R, after the stale-drop repair).
# Seed rule: the first block of five consecutive integers above 1000 not used anywhere in the
# repository's code or tests (used: 1, 2, 3, 5, 6, 11, 12, 1000). Seeds are never re-drawn.
# Construction is identical to HOLDOUT_CASES apart from the seed. Criteria: CRITERIA_VERSION
# (unchanged). Evaluations: primary at DT_S = 1.0 s; secondary at 0.25 s and 0.1 s.
# Acceptance rule (declared now): the repaired fluid engine is accepted for research use at a
# resolution dt only if every fresh-holdout run passes every criterion at that dt. Results at
# all three resolutions are reported whatever they show.
FRESH_HOLDOUT_SEEDS = (1001, 1002, 1003, 1004, 1005)
FRESH_HOLDOUT_TIMESTEPS_S = (1.0, 0.25, 0.1)


def _fresh_holdout(name: str, seed: int) -> ScenarioConfig:
    return reference_scenario(
        name,
        seed=seed,
        experiment_id="EXP-DES-FRESH",
        rate_scale=0.01,
        duration_s=2400.0,
        options=ScenarioOptions(),
    )


FRESH_HOLDOUT_CASES: tuple[ValidationCase, ...] = tuple(
    ValidationCase(
        f"fresh_{name}_seed{seed}",
        (lambda n=name, sd=seed: _fresh_holdout(n, sd)),
        schedule=periodic_schedule(("A1-B",), 100, 2400, 10, 20),
        holdout=True,
    )
    for name in ("S1", "S2", "S3")
    for seed in FRESH_HOLDOUT_SEEDS
)


@dataclass
class CaseRun:
    case: ValidationCase
    workload: Workload
    fluid: FluidRun
    des: DesResult
    comparison: CaseComparison
    outcomes: tuple[CriterionOutcome, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        return all(o.passed for o in self.outcomes)


def run_case(
    case: ValidationCase,
    service: ServiceDistribution = ServiceDistribution.DETERMINISTIC,
    service_seed: int = 0,
    dt_s: float = DT_S,
) -> CaseRun:
    workload = generate_workload(case.build())
    fluid = run_fluid(workload, case.schedule, bottleneck_groups=case.bottleneck_groups, dt_s=dt_s)
    des = simulate_des(
        workload,
        DesConfig(
            bin_s=dt_s,
            service=service,
            service_seed=service_seed,
            bottleneck_groups=case.bottleneck_groups,
        ),
        case.schedule,
    )
    comparison = compare(case.name, workload, fluid, des)
    return CaseRun(case, workload, fluid, des, comparison, evaluate(comparison, dt_s))


def comparison_table(runs: Sequence[CaseRun]) -> str:
    """Markdown table of the headline numbers for each case and route."""

    lines = [
        "| case | route | served F/D | queue MAE | RMSE | peak F/D | new amb F/D | checks F/D "
        "| recovery F/D (s) | unresolved F/D | pass |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for run in runs:
        for r in run.comparison.routes:
            rec = (
                f"{_fmt(r.recovery_fluid_s)}/{_fmt(r.recovery_des_s)}"
                if r.recovery_fluid_s is not None or r.recovery_des_s is not None
                else "-"
            )
            lines.append(
                f"| {run.case.name} | {r.route_id} | {r.served_fluid}/{r.served_des} "
                f"| {r.queue_mae:.2f} | {r.queue_rmse:.2f} | {r.peak_fluid}/{r.peak_des} "
                f"| {r.new_ambiguous_fluid}/{r.new_ambiguous_des} "
                f"| {r.check_results_fluid}/{r.check_results_des} | {rec} "
                f"| {r.unresolved_end_fluid}/{r.unresolved_end_des} "
                f"| {'yes' if run.passed else 'NO'} |"
            )
    return "\n".join(lines)


def _fmt(value: float | None) -> str:
    return "never" if value is None else f"{value:.0f}"


TIMESTEPS_S = (1.0, 0.5, 0.25, 0.1)


@dataclass(frozen=True)
class TimestepRow:
    case: str
    dt_s: float
    passed: bool
    failing: tuple[str, ...]
    peak: tuple[int, int]
    queue_mae: float
    new_ambiguous: tuple[int, int]
    unresolved: tuple[int, int]
    recovery: tuple[float | None, float | None]
    latency_p50_dev_s: float | None
    latency_p95_dev_s: float | None
    fluid_runtime_s: float
    des_runtime_s: float
    fluid_digest: str


def timestep_study(
    cases: Sequence[ValidationCase], timesteps: Sequence[float] = TIMESTEPS_S
) -> list[TimestepRow]:
    """Same workload and schedule at several fluid timesteps (DES binned to match)."""

    import time

    rows = []
    for case in cases:
        workload = generate_workload(case.build())
        for dt in timesteps:
            began = time.perf_counter()
            fluid = run_fluid(workload, case.schedule, bottleneck_groups=case.bottleneck_groups,
                              dt_s=dt)  # fmt: skip
            fluid_s = time.perf_counter() - began
            began = time.perf_counter()
            des = simulate_des(
                workload, DesConfig(bin_s=dt, bottleneck_groups=case.bottleneck_groups),
                case.schedule,
            )  # fmt: skip
            des_s = time.perf_counter() - began
            comparison = compare(case.name, workload, fluid, des)
            outcomes = evaluate(comparison, dt)
            r = comparison.routes[0]
            c = comparison

            def dev(a, b):
                return None if a is None or b is None else abs(a - b)

            rows.append(
                TimestepRow(
                    case=case.name,
                    dt_s=dt,
                    passed=all(o.passed for o in outcomes),
                    failing=tuple(sorted({o.criterion for o in outcomes if not o.passed})),
                    peak=(r.peak_fluid, r.peak_des),
                    queue_mae=r.queue_mae,
                    new_ambiguous=(r.new_ambiguous_fluid, r.new_ambiguous_des),
                    unresolved=(r.unresolved_end_fluid, r.unresolved_end_des),
                    recovery=(r.recovery_fluid_s, r.recovery_des_s),
                    latency_p50_dev_s=dev(c.latency_p50_fluid_s, c.latency_p50_des_s),
                    latency_p95_dev_s=dev(c.latency_p95_fluid_s, c.latency_p95_des_s),
                    fluid_runtime_s=fluid_s,
                    des_runtime_s=des_s,
                    fluid_digest=fluid.result.digest,
                )
            )
    return rows


def timestep_table(rows: Sequence[TimestepRow]) -> str:
    lines = [
        "| case | dt (s) | pass | failing | peak F/D | queue MAE | new amb F/D | unresolved F/D "
        "| recovery F/D (s) | dp50/dp95 (s) | runtime F/D (s) |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lat = (
            "-"
            if r.latency_p50_dev_s is None
            else f"{r.latency_p50_dev_s:.2f}/{r.latency_p95_dev_s:.2f}"
        )
        lines.append(
            f"| {r.case} | {r.dt_s} | {'yes' if r.passed else 'NO'} "
            f"| {', '.join(r.failing) or '-'} "
            f"| {r.peak[0]}/{r.peak[1]} | {r.queue_mae:.2f} | {r.new_ambiguous[0]}/"
            f"{r.new_ambiguous[1]} | {r.unresolved[0]}/{r.unresolved[1]} "
            f"| {_fmt(r.recovery[0])}/{_fmt(r.recovery[1])} | {lat} "
            f"| {r.fluid_runtime_s:.1f}/{r.des_runtime_s:.1f} |"
        )
    return "\n".join(lines)


# =====================================================================================
# Stage 04S: stage04-criteria/2, check-flow accounting, classification, budget diagnostic
# =====================================================================================
#
# Criteria/1 (above) is preserved unchanged and its historical results stand. Criteria/2 was
# written on 2026-10-22 after the Stage 04R results were known; those results (seed 11 and
# seeds 1001-1005) therefore cannot count as evidence for criteria/2. Only the Stage 04S
# holdout (HOLDOUT_V2_*) does.
#
# What changed and why:
# * latency_quantiles: criteria/1 allowed 1.5 * dt, which assumes timestep quantisation is the
#   only source of latency error. Stage 04R showed a dt-independent structural difference
#   (fluid service is shared within an interval; the DES serves one request at a time), so the
#   tolerance shrank towards zero while the difference did not. Criteria/2 adds a relative term
#   on the reference (DES) quantile: |dq| <= 1.5 * dt + 0.05 * q_DES. The relative term is half
#   the 10 % relative tolerance criteria/1 already accepts for queue depth, backlog and
#   ambiguity, because a latency quantile aggregates the whole run. It is additive, not a max,
#   and proportional to the quantile itself, so at low latency (p50 about 0.3-0.5 s) it adds at
#   most 0.025 s and the tolerance stays at the quantisation floor.
# * check_results is kept unchanged, and five check-flow measures are added (below). A
#   check-count difference caused by the eligible pool running out is attributed, not
#   forgiven: it still fails check_results and keeps the run out of GREEN.
# * recovery_time is kept unchanged (official 2.0 s threshold). A threshold-sensitivity flag is
#   added; it never turns a failure into a pass.
# Unchanged: conservation, served_total, queue_mae, queue_peak, new_ambiguous_total,
# unresolved_at_end, check_results, recovery_time, classification_agreement.

CRITERIA_V2_VERSION = "stage04-criteria/2"
LATENCY_RELATIVE_TOLERANCE = 0.05
ANSWER_RATE_TOLERANCE = 0.05
THRESHOLD_JUMP_S = 30.0

CRITERIA_V2: tuple[Criterion, ...] = (
    *(c for c in CRITERIA if c.name not in ("latency_quantiles",)),
    Criterion(
        "latency_quantiles_v2",
        "|p50_des - p50_fluid| <= 1.5 * dt + 0.05 * p50_des and the same for p95 "
        "(when both have >= 20 timely payments)",
        "Quantisation floor (criteria/1) plus a dt-independent structural term relative to the "
        "reference quantile; see the module comment for the derivation.",
    ),
    Criterion(
        "check_flow_conservation",
        "per model: proposed == released + shortfall; released == enqueued; released == answered "
        "+ timed out + in flight at the horizon, with 0 <= in flight <= checks released in the "
        "final check_timeout_s",
        "Every proposed check is accounted for exactly once; a residual is a defect.",
    ),
    Criterion(
        "released_alignment",
        "|released_fluid - released_des| <= max(3, 0.10 * released_des)",
        "Same form as check_results/1, applied to releases (pool supply) instead of answers.",
    ),
    Criterion(
        "answer_rate_alignment",
        "|answered/released (fluid) - answered/released (des)| <= 0.05 (when both release >= 20)",
        "Separates the per-check physics (does a released check get answered in time?) from the "
        "pool supply, which depends on the unresolved backlog.",
    ),
)


@dataclass(frozen=True)
class CheckFlow:
    """Status-check flow of one model run. Every field is observable from model outputs."""

    proposed: int
    released: int
    shortfall: int
    pool_limited_releases: int  # release events that got fewer checks than proposed
    enqueued: int
    completed: int
    dropped: int
    answered: int
    timed_out: int
    in_flight_end: int
    released_final_window: int

    @property
    def conserved(self) -> bool:
        return (
            self.proposed == self.released + self.shortfall
            and self.released == self.enqueued
            and self.released == self.answered + self.timed_out + self.in_flight_end
            and 0 <= self.in_flight_end <= self.released_final_window
        )


def _proposals(schedule: Sequence[CheckRelease], bin_s: float, bins: int) -> list[int]:
    out = [0] * bins
    for release in schedule:
        k = round(release.time_s / bin_s)
        if k < bins:
            out[k] += release.count
    return out


def check_flow_fluid(
    result: SimulationResult, schedule: Sequence[CheckRelease], check_timeout_s: float
) -> CheckFlow:
    bin_s = result.config.dt_s
    proposed = _proposals(schedule, bin_s, result.ticks)
    released = [0] * result.ticks
    for m in result.metrics:
        released[m.tick] += m.checks_released
    window = math.ceil(check_timeout_s / bin_s)
    return CheckFlow(
        proposed=sum(proposed),
        released=sum(released),
        shortfall=result.total("checks_denied"),
        pool_limited_releases=sum(1 for p, r in zip(proposed, released, strict=True) if r < p),
        enqueued=result.total("arrived_status_check"),
        completed=result.total("completed_status_check"),
        dropped=result.total("dropped_status_check"),
        answered=result.total("check_results"),
        timed_out=result.total("wasted_checks"),
        in_flight_end=result.total("checks_released")
        - result.total("check_results")
        - result.total("wasted_checks"),
        released_final_window=sum(released[-window:]),
    )


def check_flow_des(
    result: DesResult, schedule: Sequence[CheckRelease], check_timeout_s: float
) -> CheckFlow:
    bin_s = result.config.bin_s
    proposed = _proposals(schedule, bin_s, result.bins)
    released = [0] * result.bins
    for i in result.intervals:
        released[i.bin_index] += i.checks_released
    window = math.ceil(check_timeout_s / bin_s)
    return CheckFlow(
        proposed=sum(proposed),
        released=sum(released),
        shortfall=sum(max(0, p - r) for p, r in zip(proposed, released, strict=True)),
        pool_limited_releases=sum(1 for p, r in zip(proposed, released, strict=True) if r < p),
        enqueued=result.total("arrived_status_check"),
        completed=result.total("completed_status_check"),
        dropped=result.total("dropped_status_check"),
        answered=result.total("check_results"),
        timed_out=result.total("wasted_checks"),
        in_flight_end=result.total("checks_released")
        - result.total("check_results")
        - result.total("wasted_checks"),
        released_final_window=sum(released[-window:]),
    )


def threshold_sensitive(route: RouteComparison) -> bool:
    """Recovery estimate unstable across 2.0/2.5/3.0 s in either model (labelled flag)."""

    for side in (1, 2):
        values = [row[side] for row in route.recovery_sensitivity]
        for a, b in zip(values, values[1:], strict=False):
            if (a is None) != (b is None):
                return True
            if a is not None and abs(a - b) > THRESHOLD_JUMP_S:
                return True
    return False


def evaluate_v2(
    comparison: CaseComparison,
    flow_fluid: CheckFlow,
    flow_des: CheckFlow,
    dt: float = DT_S,
) -> tuple[CriterionOutcome, ...]:
    """Criteria/2: criteria/1 minus latency_quantiles, plus the five criteria above."""

    kept = [o for o in evaluate(comparison, dt) if o.criterion != "latency_quantiles"]
    out = list(kept)
    case = comparison.case

    def add(name, passed, detail):
        out.append(CriterionOutcome(case, None, name, bool(passed), detail))

    if min(comparison.latency_samples) >= 20:
        ok, parts = True, []
        for label, f, d in (
            ("p50", comparison.latency_p50_fluid_s, comparison.latency_p50_des_s),
            ("p95", comparison.latency_p95_fluid_s, comparison.latency_p95_des_s),
        ):
            limit = 1.5 * dt + LATENCY_RELATIVE_TOLERANCE * d
            ok = ok and abs(f - d) <= limit
            parts.append(f"|d{label}| {abs(f - d):.3f} s (limit {limit:.3f})")
        add("latency_quantiles_v2", ok, ", ".join(parts))
    add(
        "check_flow_conservation",
        flow_fluid.conserved and flow_des.conserved,
        f"fluid {flow_fluid}, des {flow_des}",
    )
    limit = max(3, 0.10 * flow_des.released)
    add(
        "released_alignment",
        abs(flow_fluid.released - flow_des.released) <= limit,
        f"{flow_fluid.released} vs {flow_des.released} (limit {limit:.1f}); pool-limited "
        f"releases {flow_fluid.pool_limited_releases}/{flow_des.pool_limited_releases}",
    )
    if min(flow_fluid.released, flow_des.released) >= 20:
        rf = flow_fluid.answered / flow_fluid.released
        rd = flow_des.answered / flow_des.released
        add("answer_rate_alignment", abs(rf - rd) <= ANSWER_RATE_TOLERANCE,
            f"{rf:.3f} vs {rd:.3f}")  # fmt: skip
    return tuple(out)


# ------------------------------------------------------------- budget diagnostic


BUDGET_CANDIDATES = (0, 5, 20, 50)  # checks proposed per route every 10 s from t = 100 s
BUDGET_OBJECTIVE = "J = new_ambiguous + unresolved_at_end + 0.05 * checks_released"
MATERIAL_REGRET_ABS = 5.0
MATERIAL_REGRET_REL = 0.05


def budget_objective(new_ambiguous: int, unresolved: int, released: int) -> float:
    """Simple diagnostic objective (fewer ambiguous and unresolved payments, checks cost a
    little). Not the AORTS objective; only used to test whether the models rank budgets alike."""

    return new_ambiguous + unresolved + 0.05 * released


@dataclass(frozen=True)
class BudgetOutcome:
    budget: int
    objective: float
    new_ambiguous: int
    unresolved: int
    released: int
    mean_queue: float


@dataclass(frozen=True)
class BudgetDiagnostic:
    fluid: tuple[BudgetOutcome, ...]
    des: tuple[BudgetOutcome, ...]
    best_fluid: int
    best_des: int
    regret: float  # J_des(fluid's choice) - J_des(DES choice)
    material_reversal: bool
    sign_disagreements: tuple[str, ...]

    @property
    def ranking_agrees(self) -> bool:
        return self.best_fluid == self.best_des and not self.sign_disagreements


def _deadband_sign(delta: float, base: float, floor: float) -> int:
    band = max(floor, 0.02 * abs(base))
    return 0 if abs(delta) <= band else (1 if delta > 0 else -1)


def budget_diagnostic(
    fluid: Sequence[BudgetOutcome], des: Sequence[BudgetOutcome]
) -> BudgetDiagnostic:
    def best(rows):  # lowest objective; ties go to the smallest budget
        return min(rows, key=lambda r: (r.objective, r.budget)).budget

    bf, bd = best(fluid), best(des)
    by_budget = {r.budget: r for r in des}
    regret = by_budget[bf].objective - by_budget[bd].objective
    material = regret > max(MATERIAL_REGRET_ABS, MATERIAL_REGRET_REL * by_budget[bd].objective)
    lo, hi = min(BUDGET_CANDIDATES), max(BUDGET_CANDIDATES)
    f0, f1 = (next(r for r in fluid if r.budget == b) for b in (lo, hi))
    d0, d1 = (next(r for r in des if r.budget == b) for b in (lo, hi))
    disagreements = []
    for name, floor in (("mean_queue", 0.5), ("new_ambiguous", 2.0), ("unresolved", 2.0)):
        sf = _deadband_sign(getattr(f1, name) - getattr(f0, name), getattr(f0, name), floor)
        sd = _deadband_sign(getattr(d1, name) - getattr(d0, name), getattr(d0, name), floor)
        if sf != sd:
            disagreements.append(f"{name}: fluid {sf:+d}, des {sd:+d}")
    return BudgetDiagnostic(
        tuple(fluid), tuple(des), bf, bd, regret, material, tuple(disagreements)
    )


# --------------------------------------------------------------- classification


class ModelClass(StrEnum):
    GREEN = "GREEN"
    AMBER = "AMBER"
    RED = "RED"


_CLASS_ORDER = {ModelClass.GREEN: 0, ModelClass.AMBER: 1, ModelClass.RED: 2}


def worst(classes: Sequence["ModelClass"]) -> "ModelClass":
    return max(classes, key=lambda c: _CLASS_ORDER[c])


def classify_run(
    comparison: CaseComparison,
    outcomes_v2: Sequence[CriterionOutcome],
    diagnostic: BudgetDiagnostic | None,
    dt: float = DT_S,
) -> tuple[ModelClass, tuple[str, ...]]:
    """Frozen GREEN/AMBER/RED rule for one run (declared before the Stage 04S holdout).

    RED: any conservation failure (work or check flow); a material budget-ranking reversal;
    unresolved-at-end or new-ambiguous error above twice its criteria tolerance;
    classification agreement below 0.90.
    GREEN: every criteria/2 outcome passes and the budget diagnostic agrees completely (same
    best budget, no direction disagreement).
    AMBER: everything else (conservation holds, errors exceed the preferred tolerances or the
    rankings differ without material regret).
    """

    reasons: list[str] = []
    red = False
    failed = {o.criterion for o in outcomes_v2 if not o.passed}
    if {"conservation", "check_flow_conservation"} & failed:
        red = True
        reasons.append("accounting failure")
    for r in comparison.routes:
        c = r.capacity_max_rps
        if abs(r.unresolved_end_des - r.unresolved_end_fluid) > 2 * max(
            c * dt + 2, 0.10 * r.unresolved_end_fluid
        ):
            red = True
            reasons.append(f"{r.route_id}: unresolved error > 2x tolerance")
        if abs(r.new_ambiguous_des - r.new_ambiguous_fluid) > 2 * max(
            2 * c * dt + 5, 0.10 * r.new_ambiguous_fluid
        ):
            red = True
            reasons.append(f"{r.route_id}: new-ambiguous error > 2x tolerance")
    share = comparison.classification_agreement
    if share is not None and comparison.classified_payments >= 20 and share < 0.90:
        red = True
        reasons.append(f"classification agreement {share:.3f} < 0.90")
    if diagnostic is not None and diagnostic.material_reversal:
        red = True
        reasons.append(
            f"material budget reversal: fluid picks {diagnostic.best_fluid}, DES "
            f"{diagnostic.best_des}, regret {diagnostic.regret:.1f}"
        )
    if red:
        return ModelClass.RED, tuple(reasons)
    if diagnostic is None:
        reasons.append("no budget diagnostic: ranking agreement unverified")
    if failed:
        reasons.append("criteria/2 failures: " + ", ".join(sorted(failed)))
    if diagnostic is not None and not diagnostic.ranking_agrees:
        reasons.append(
            f"budget ranking differs without material regret (best {diagnostic.best_fluid} vs "
            f"{diagnostic.best_des}; {'; '.join(diagnostic.sign_disagreements) or 'same signs'})"
        )
    if reasons:
        return ModelClass.AMBER, tuple(reasons)
    return ModelClass.GREEN, ()


@dataclass(frozen=True)
class V2Record:
    """One (scenario, seed, dt) evaluation under criteria/2 across all budget candidates."""

    case: str
    dt_s: float
    model_class: ModelClass
    reasons: tuple[str, ...]
    failing: tuple[tuple[int, tuple[str, ...]], ...]  # (budget, failing criteria)
    diagnostic: BudgetDiagnostic
    flows: tuple[tuple[int, CheckFlow, CheckFlow], ...]
    threshold_sensitive: bool
    recovery_sensitivity: tuple[tuple[float, float | None, float | None], ...]
    reference: RouteComparison  # the 20-check reference schedule
    latency: tuple[float | None, float | None, float | None, float | None]
    fluid_digests: tuple[str, ...]
    des_digests: tuple[str, ...]
    runtime_fluid_s: float
    runtime_des_s: float


def budget_schedule(budget: int, routes: Sequence[str], horizon_s: float) -> tuple:
    if budget == 0:
        return ()
    return periodic_schedule(routes, 100, int(horizon_s), 10, budget)


def run_v2(case: ValidationCase, dt: float) -> V2Record:
    """Criteria/2 + budget diagnostic + classification for one case at one timestep."""

    import time
    from dataclasses import replace

    workload = generate_workload(case.build())
    scenario = workload.manifest.config
    routes = tuple(r.route_id for r in scenario.routes)
    timeout = scenario.timing_policy.check_timeout_s
    per_class, failing, flows, outs_f, outs_d = [], [], [], [], []
    fdig, ddig, tf, td = [], [], 0.0, 0.0
    reference = None
    for budget in BUDGET_CANDIDATES:
        schedule = budget_schedule(budget, routes, scenario.duration_s)
        variant = replace(case, schedule=schedule)
        began = time.perf_counter()
        fluid = run_fluid(workload, schedule, bottleneck_groups=variant.bottleneck_groups,
                          dt_s=dt)  # fmt: skip
        tf += time.perf_counter() - began
        began = time.perf_counter()
        des_config = DesConfig(bin_s=dt, bottleneck_groups=variant.bottleneck_groups)
        des = simulate_des(workload, des_config, schedule)
        td += time.perf_counter() - began
        comparison = compare(case.name, workload, fluid, des)
        ff = check_flow_fluid(fluid.result, schedule, timeout)
        fd = check_flow_des(des, schedule, timeout)
        outcomes = evaluate_v2(comparison, ff, fd, dt)
        failing.append((budget, tuple(sorted(o.criterion for o in outcomes if not o.passed))))
        flows.append((budget, ff, fd))
        per_class.append((comparison, outcomes))
        fdig.append(fluid.result.digest)
        ddig.append(des.digest)
        for rows, res, f in ((outs_f, fluid.result, True), (outs_d, des, False)):
            route_rows = (
                [m for m in res.metrics if m.route_id == routes[0]]
                if f
                else [i for i in res.intervals if i.route_id == routes[0]]
            )
            rows.append(
                BudgetOutcome(
                    budget=budget,
                    objective=budget_objective(
                        res.total("new_ambiguous"),
                        res.total("unresolved_at_end"),
                        res.total("checks_released"),
                    ),  # fmt: skip
                    new_ambiguous=res.total("new_ambiguous"),
                    unresolved=res.total("unresolved_at_end"),
                    released=res.total("checks_released"),
                    mean_queue=sum(r.queue_depth for r in route_rows) / len(route_rows),
                )
            )
        if budget == 20:
            reference = comparison
    diagnostic = budget_diagnostic(outs_f, outs_d)
    classes, reasons = [], []
    for comparison, outcomes in per_class:
        cls, why = classify_run(comparison, outcomes, diagnostic, dt)
        classes.append(cls)
        reasons.extend(why)
    ref_route = reference.routes[0]
    return V2Record(
        case=case.name,
        dt_s=dt,
        model_class=worst(classes),
        reasons=tuple(dict.fromkeys(reasons)),
        failing=tuple(failing),
        diagnostic=diagnostic,
        flows=tuple(flows),
        threshold_sensitive=threshold_sensitive(ref_route),
        recovery_sensitivity=ref_route.recovery_sensitivity,
        reference=ref_route,
        latency=(
            reference.latency_p50_fluid_s,
            reference.latency_p50_des_s,
            reference.latency_p95_fluid_s,
            reference.latency_p95_des_s,
        ),
        fluid_digests=tuple(fdig),
        des_digests=tuple(ddig),
        runtime_fluid_s=tf,
        runtime_des_s=td,
    )


# ------------------------------------------------------------ Stage 04S holdout
# DECLARED before any run of these seeds. Seed rule: the first block of ten consecutive
# integers >= 2001 not used anywhere in the repository (used: 1, 2, 3, 5, 6, 11, 12, 1000,
# 1001-1005). Ten seeds per principal scenario (S1, S2, S3), paired across models, budgets and
# timesteps. Construction identical to HOLDOUT_CASES apart from the seed. Criteria: criteria/2.
# Classification: classify_run (worst over the four budget candidates), regime class = worst
# over the ten seeds. Timestep rule: the recommended fluid timestep for a regime is the
# coarsest evaluated dt at which the regime is GREEN; if no dt is GREEN the fluid model is not
# permitted for primary evidence in that regime. The holdout runs once.
HOLDOUT_V2_SEEDS = tuple(range(2001, 2011))
HOLDOUT_V2_TIMESTEPS_S = (1.0, 0.25, 0.1)
HOLDOUT_V2_SCENARIOS = ("S1", "S2", "S3")
HOLDOUT_V2_PRIMARY_METRICS = (
    "unresolved_at_end",
    "new_ambiguous_total",
    "queue_mae",
    "queue_peak",
    "released_alignment",
    "answer_rate_alignment",
    "latency_quantiles_v2",
    "recovery_time",
    "budget ranking (best budget, regret, direction of effects)",
)


def _holdout_v2(name: str, seed: int) -> ScenarioConfig:
    return reference_scenario(
        name,
        seed=seed,
        experiment_id="EXP-DES-V2",
        rate_scale=0.01,
        duration_s=2400.0,
        options=ScenarioOptions(),
    )


HOLDOUT_V2_CASES: tuple[ValidationCase, ...] = tuple(
    ValidationCase(
        f"v2_{name}_seed{seed}",
        (lambda n=name, sd=seed: _holdout_v2(n, sd)),
        schedule=periodic_schedule(("A1-B",), 100, 2400, 10, 20),
        holdout=True,
    )
    for name in HOLDOUT_V2_SCENARIOS
    for seed in HOLDOUT_V2_SEEDS
)


def holdout_v2_declaration() -> dict:
    """Machine-readable declaration; its hash identifies the experiment configuration."""

    return {
        "criteria": CRITERIA_V2_VERSION,
        "criteria_rules": [(c.name, c.rule) for c in CRITERIA_V2],
        "latency_relative_tolerance": LATENCY_RELATIVE_TOLERANCE,
        "answer_rate_tolerance": ANSWER_RATE_TOLERANCE,
        "threshold_jump_s": THRESHOLD_JUMP_S,
        "seeds": list(HOLDOUT_V2_SEEDS),
        "scenarios": list(HOLDOUT_V2_SCENARIOS),
        "rate_scale": 0.01,
        "duration_s": 2400.0,
        "reference_schedule": "20 checks per route every 10 s from t = 100 s",
        "timesteps_s": list(HOLDOUT_V2_TIMESTEPS_S),
        "budget_candidates": list(BUDGET_CANDIDATES),
        "budget_objective": BUDGET_OBJECTIVE,
        "material_regret": [MATERIAL_REGRET_ABS, MATERIAL_REGRET_REL],
        "classification": classify_run.__doc__,
        "timestep_rule": "coarsest dt at which the regime is GREEN over all seeds",
        "primary_metrics": list(HOLDOUT_V2_PRIMARY_METRICS),
        "recovery_threshold_s": RECOVERY_DELAY_THRESHOLD_S,
        "recovery_sensitivity_s": list(RECOVERY_SENSITIVITY_THRESHOLDS_S),
    }


def main() -> None:
    """Print the development and holdout comparison evidence (deterministic)."""

    print(f"criteria {CRITERIA_VERSION}")
    for title, cases in (("development", DEVELOPMENT_CASES), ("holdout", HOLDOUT_CASES)):
        runs = [run_case(case) for case in cases]
        print(f"\n## {title}\n")
        print(comparison_table(runs))
        for run in runs:
            for outcome in run.outcomes:
                if not outcome.passed:
                    print(f"FAIL {run.case.name} {outcome.criterion} {outcome.detail}")


if __name__ == "__main__":
    main()
