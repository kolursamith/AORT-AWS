"""Independent validation of the Stage 07 look-ahead against matched DES runs.

Protocol for one case (route, decision instant ``t0``, horizon ``H``, budgets ``B``):

1. Prefix: the DES runs the workload up to ``t0``. Optional probe checks (one every
   ``probe_every_s`` seconds during the last ``probe_window_s`` before ``t0``, strict timing
   policy) give the controller observed check outcomes, as a real controller would have.
2. Observation: from the prefix run only, the remitter-observable view is built: own-traffic
   telemetry (Stage 06 ``des_telemetry``) through a Route Twin into the Stage 06 estimator, and
   transaction facts from ``DesObservation`` (ambiguity times, resolutions, check answers).
   Send time is ambiguity time minus the client timeout; eligibility follows the timing policy.
3. Forecast: ``forecast_budgets`` for every budget, plus the NO_FEEDBACK and NO_REPAY
   ablations, from that observable state only.
4. Outcome: for each budget, an independent DES run of the same workload with the same prefix
   schedule plus ``b`` checks released every second over ``[t0, t0 + H)`` (oldest eligible
   first, the forecast's release order). The DES is deterministic, so the prefix is identical.
5. Scoring (the only place truth is read): DES intervals and traces over ``[t0, t0 + H)``.

Actual measures: mean bottleneck depth; new ambiguous payments; checks released and answered
in time; ambiguous payments resolved by a check; ambiguous backlog at ``t0 + H`` (observable);
mean payment wait = mean depth / true capacity (Little's law, scorer side) and the mean latency
of payments sent in the window and answered by its end (right-censored, reported separately).
Comparisons: errors of the CENTRAL forecast; whether the actual value lies inside the
[OPTIMISTIC, PESSIMISTIC] band; directional agreement of each budget's change against zero
(differences below max(0.5, 2 % of the zero-budget value) count as no change); Kendall rank
agreement of budgets by new ambiguity. These are development smoke results, not held-out evidence.
"""

import hashlib
import json
import math
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path

from contracts.v1_1 import SCHEMA_VERSION, ValidatedRouteTelemetry
from intelligence.common.clock import ManualClock
from intelligence.controller.lookahead import (
    Band,
    CheckCandidate,
    CheckEvidence,
    CheckOutcomeCounts,
    FeedbackMode,
    ForecastInput,
    ForecastResult,
    LookaheadConfig,
    forecast_budgets,
)
from intelligence.data.generate_dataset import generate_workload
from intelligence.experiments import capacity_evaluation as ce
from intelligence.ml.capacity_estimator import CapacityEstimator, RouteState
from intelligence.simulation.des_validate import CheckRelease, DesConfig, simulate_des
from intelligence.simulation.model_selection import Purpose, select_model
from intelligence.simulation.scenarios import (
    RepaySpec,
    ScenarioConfig,
    ScenarioOptions,
    reference_scenario,
)
from intelligence.twin.route_twin import RouteTwinRegistry

SEEDS = (7101, 7102)
SCALE = 0.02
HORIZON_S = 60.0
DIRECTION_TOLERANCE = 0.5  # expected transactions or requests
RELATIVE_TOLERANCE = 0.02  # of the zero-budget value: smaller changes count as no change


@dataclass(frozen=True)
class LookaheadCase:
    name: str
    description: str
    config: ScenarioConfig
    route_id: str
    decision_s: float
    budgets: tuple[int, ...]
    probe_every_s: float = 2.0
    probe_window_s: float = 120.0
    freeze: tuple[float, float] | None = None  # telemetry not delivered in [start, end)
    flag_last_s: float = 0.0  # final windows delivered as flagged (STATISTICAL_ANOMALY)


def cases(seed: int, scale: float = SCALE) -> list[LookaheadCase]:
    """Eleven declared designs. S1/S2/S3 are used unchanged where they appear."""

    def ref(name, duration, **options):
        return reference_scenario(
            name,
            seed=seed,
            experiment_id="EXP-S07",
            rate_scale=scale,
            duration_s=duration,
            options=ScenarioOptions(**options),
        )

    def custom(name, routes, faults=(), duration=1500.0, repay=None):
        config = ce._custom(name, seed, routes, faults, scale=scale, duration=duration)
        config = config.model_copy(update={"experiment_id": "EXP-S07"})
        if repay is not None:
            config = config.model_copy(update={"repay": RepaySpec(probability=repay)})
        return config

    one = (("A1-B", "B", 0.7, 1.0),)
    small = (0, 1, 3, 8)
    return [
        LookaheadCase(
            "no_load",
            "no payments at all",
            custom("s07-noload", (("A1-B", "B", 0.0, 1.0),)),
            "A1-B",
            600.0,
            small,
        ),
        LookaheadCase(
            "low_load",
            "rho 0.3, healthy",
            custom("s07-low", (("A1-B", "B", 0.3, 1.0),)),
            "A1-B",
            600.0,
            small,
        ),
        LookaheadCase(
            "near_saturation",
            "rho 0.95, healthy",
            custom("s07-near", (("A1-B", "B", 0.95, 1.0),)),
            "A1-B",
            600.0,
            small,
        ),
        LookaheadCase(
            "prolonged_capacity_loss",
            "S2, decision 600 s into the fault",
            ref("S2", 1500.0),
            "A1-B",
            1200.0,
            small,
        ),
        LookaheadCase(
            "status_check_burst",
            "30 % lost confirmations, healthy capacity, bursts",
            custom("s07-burst", one, (ce._cut("A1-B", 100.0, 1500.0, 0.3, "RESPONSE_LOSS"),)),
            "A1-B",
            600.0,
            (0, 5, 12, 25),
        ),
        LookaheadCase(
            "repayment_feedback",
            "rho 0.7, capacity -35 %, every customer re-pays",
            custom("s07-repay", one, (ce._cut("A1-B", 400.0, 1500.0, 0.35),), repay=1.0),
            "A1-B",
            700.0,
            small,
        ),
        LookaheadCase(
            "lost_confirmation_recovery",
            "RV-1 (Stage 06), decision during the fault",
            ce.recovery_valuable_design(seed, scale=scale).model_copy(
                update={"experiment_id": "EXP-S07"}
            ),
            "A1-B",
            700.0,
            (0, 1, 3, 8),
        ),
        LookaheadCase(
            "stale_telemetry",
            "S2; no telemetry for the 60 s before the decision",
            ref("S2", 1500.0),
            "A1-B",
            1200.0,
            small,
            freeze=(1140.0, 1200.0),
        ),
        LookaheadCase(
            "poisoning_flag",
            "S2; the last 5 windows arrive flagged",
            ref("S2", 1500.0),
            "A1-B",
            1200.0,
            small,
            flag_last_s=5.0,
        ),
        LookaheadCase(
            "sudden_capacity_drop",
            "rho 0.7, capacity -50 % 5 s before the decision",
            custom("s07-drop", one, (ce._cut("A1-B", 595.0, 1500.0, 0.5),)),
            "A1-B",
            600.0,
            small,
        ),
        LookaheadCase(
            "healthy_parallel_route",
            "S2 with A1-C; forecast for the healthy A1-C",
            ref("S2", 1500.0, healthy_parallel_route=True),
            "A1-C",
            1200.0,
            small,
        ),
    ]


# ----------------------------------------------------------------- observation (no truth)


def probe_schedule(case: LookaheadCase) -> tuple[CheckRelease, ...]:
    start = max(0.0, case.decision_s - case.probe_window_s)
    times = []
    t = start
    while t < case.decision_s - 1e-9:
        times.append(t)
        t += case.probe_every_s
    return tuple(CheckRelease(round(t, 6), case.route_id, 1) for t in times)


@dataclass
class ObservedState:
    forecast_input: ForecastInput
    route_states: dict[float, RouteState]
    probes_answered: int


def observe(case: LookaheadCase, workload) -> ObservedState:
    """Observable state at ``t0`` from a prefix run (remitter-visible data only)."""

    config = case.config
    policy = config.timing_policy
    t0 = case.decision_s
    prefix = simulate_des(workload, DesConfig(bin_s=1.0, horizon_s=t0), probe_schedule(case))
    routes = [r.route_id for r in config.routes]
    telemetry = ce.des_telemetry(
        ce.remitter_view(prefix),
        experiment_id=config.experiment_id,
        routes=routes,
        epoch=config.epoch,
        horizon_s=t0,
        client_timeout_s=policy.client_timeout_s,
    )[case.route_id]

    clock = ManualClock(config.epoch)
    twin = RouteTwinRegistry(ce.twin_config(), clock)
    source = f"sim-remitter-{case.route_id}"
    twin.register_route(config.route(case.route_id).descriptor(config.experiment_id), [source])
    estimator = CapacityEstimator(config.experiment_id, case.route_id)
    states: dict[float, RouteState] = {}
    estimate = None
    for obs in telemetry:
        end = obs.observation_window_end
        clock.set_time(end)
        t_end = (end - config.epoch).total_seconds()
        frozen = case.freeze is not None and case.freeze[0] <= t_end - 1.0 < case.freeze[1]
        if not frozen:
            flags = ("STATISTICAL_ANOMALY",) if t_end > t0 - case.flag_last_s + 1e-9 else ()
            twin.ingest(
                ValidatedRouteTelemetry(
                    schema_version=SCHEMA_VERSION,
                    experiment_id=obs.experiment_id,
                    route_id=obs.route_id,
                    telemetry=obs,
                    trust_status="AUTHENTICATED",
                    validator_id="eval-validator",
                    validator_version="eval-1",
                    validated_at=obs.observation_timestamp,
                    validation_flags=flags,
                    cross_check_status="UNAVAILABLE",
                )
            )
        estimate = estimator.update(twin.snapshot(config.experiment_id, case.route_id), end)
        states[t_end] = estimate.route_state

    # transaction facts from observations
    ambiguous_at: dict[str, float] = {}
    last_check: dict[str, tuple[float, str]] = {}
    checks_used: dict[str, int] = defaultdict(int)
    counts = CheckOutcomeCounts()
    late_resolutions = 0
    window_start = t0 - 60.0
    outstanding_series = []
    answered = 0
    for o in prefix.observations:
        for txn in o.newly_ambiguous:
            ambiguous_at[txn] = o.end_s  # observed at the bin end (conservative)
        for _txn, _, how, at in o.resolutions:
            if how != "TIMELY_RESPONSE" and at >= window_start:
                late_resolutions += 1
        for _, txn, result, at in o.check_results:
            last_check[txn] = (at, result)
            answered += 1
            counts = counts.record(_result_kind(result))
        for _ in o.check_timeouts:
            counts = counts.record(None)
        if o.end_s > window_start:
            outstanding_series.append(len(dict(o.ambiguous_outstanding).get(case.route_id, ())))
    # checks used: count answered/timed-out releases per transaction from check results only
    for o in prefix.observations:
        for _, txn, _, _ in o.check_results:
            checks_used[txn] += 1
    final = dict(prefix.observations[-1].ambiguous_outstanding).get(case.route_id, ())
    mean_outstanding = (
        sum(outstanding_series) / len(outstanding_series) if outstanding_series else 0.0
    )
    hazard = late_resolutions / (mean_outstanding * 60.0) if mean_outstanding > 0 else None

    decision_time = config.epoch + timedelta(seconds=t0)
    candidates = []
    for txn in sorted(final, key=lambda t: (ambiguous_at.get(t, 0.0), t)):
        amb = ambiguous_at.get(txn)
        if amb is None:
            continue
        sent = amb - policy.client_timeout_s
        eligible = sent + policy.first_check_delay_s
        evidence = CheckEvidence.NO_CHECK_YET
        if txn in last_check:
            at, result = last_check[txn]
            eligible = max(eligible, at + policy.min_check_spacing_s)
            evidence = {
                "UNKNOWN": CheckEvidence.LAST_CHECK_UNKNOWN,
                "PENDING": CheckEvidence.LAST_CHECK_PENDING,
            }.get(result, CheckEvidence.NO_CHECK_YET)
        if checks_used[txn] >= policy.max_counted_checks:
            continue
        congested = states.get(float(math.floor(sent) + 1.0)) is RouteState.CONGESTED
        candidates.append(
            CheckCandidate(
                experiment_id=config.experiment_id,
                route_id=case.route_id,
                transaction_id=txn,
                version=1 + checks_used[txn],
                eligible_at=config.epoch + timedelta(seconds=eligible),
                checks_used=checks_used[txn],
                deadline_at=config.epoch + timedelta(seconds=sent + 3600.0),
                evidence=evidence,
                sent_while_congested=congested,
            )
        )
    # the DES releases oldest-ambiguous-first among eligible: order by eligibility, then age
    candidates.sort(key=lambda c: (c.eligible_at, c.transaction_id))
    last = telemetry[-1] if telemetry else None
    recent = [
        t.p50_latency_ms
        for t in telemetry[-10:]
        if t.p50_latency_ms is not None and t.latency_sample_count >= 3
    ]
    if case.freeze is not None and case.freeze[1] >= t0:
        recent = []  # nothing was delivered recently
    median_latency = sorted(recent)[len(recent) // 2] / 1000.0 if recent else None
    windows = [] if (case.freeze is not None and case.freeze[1] >= t0) else telemetry[-10:]
    timeouts = sum(t.timeout_count or 0 for t in windows)
    determined = sum(t.timeout_denominator_count or 0 for t in windows)
    timeout_rate = timeouts / determined if determined else None
    forecast_input = ForecastInput(
        experiment_id=config.experiment_id,
        route_id=case.route_id,
        decision_time=decision_time,
        capacity=estimate,
        observed_outstanding=None if last is None else last.queue_depth,
        current_check_rps=0.0,  # des_telemetry excludes checks from offered load
        ambiguous_backlog=len(final),
        late_resolution_hazard_per_s=hazard,
        candidates=tuple(candidates),
        check_outcomes=counts,
        observed_median_latency_s=median_latency,
        observed_timeout_rate=timeout_rate,
    )
    return ObservedState(forecast_input, states, answered)


def _result_kind(result: str):
    from contracts.v1_1 import CheckResultKind

    return CheckResultKind(result)


# ----------------------------------------------------------------- outcomes (truth: scorer)


@dataclass(frozen=True)
class Actual:
    budget_rps: int
    queue_mean: float
    new_ambiguous: float
    checks_released: float
    checks_on_time: float
    resolved_by_check: float
    backlog_end: float
    mean_wait_littles_s: float
    answered_payment_latency_s: float | None


def actual_outcome(case: LookaheadCase, workload, budget: int) -> Actual:
    t0, h = case.decision_s, HORIZON_S
    burst = tuple(CheckRelease(t0 + k, case.route_id, budget) for k in range(int(h)) if budget)
    result = simulate_des(
        workload, DesConfig(bin_s=1.0, horizon_s=t0 + h), probe_schedule(case) + burst
    )
    rows = [i for i in result.intervals if i.route_id == case.route_id and t0 <= i.start_s < t0 + h]
    capacity = ce.true_capacity(workload, case.route_id, t0, t0 + h)
    depth = sum(i.bottleneck_queue_depth for i in rows) / len(rows)
    latencies = [
        t.response_s - t.sent_s
        for t in result.traces
        if t.route_id == case.route_id and t0 <= t.sent_s < t0 + h and t.response_s is not None
    ]
    by_check = sum(
        1
        for t in result.traces
        if t.route_id == case.route_id
        and t.resolved_how == "CHECK"
        and t.resolved_s is not None
        and t0 <= t.resolved_s < t0 + h
    )
    final = dict(result.observations[-1].ambiguous_outstanding).get(case.route_id, ())
    return Actual(
        budget_rps=budget,
        queue_mean=depth,
        new_ambiguous=float(sum(i.new_ambiguous for i in rows)),
        checks_released=float(sum(i.checks_released for i in rows)),
        checks_on_time=float(sum(i.check_results for i in rows)),
        resolved_by_check=float(by_check),
        backlog_end=float(len(final)),
        mean_wait_littles_s=depth / capacity if capacity > 0 else math.inf,
        answered_payment_latency_s=sum(latencies) / len(latencies) if latencies else None,
    )


# ----------------------------------------------------------------- comparison


def _sign(x: float, scale: float = 0.0) -> int:
    tol = max(DIRECTION_TOLERANCE, RELATIVE_TOLERANCE * abs(scale))
    return 0 if abs(x) < tol else (1 if x > 0 else -1)


def kendall(a: list[float], b: list[float]) -> float | None:
    """Share of budget pairs ordered the same way (ties within tolerance are skipped)."""

    pairs = concordant = 0
    scale_a, scale_b = max(map(abs, a), default=0.0), max(map(abs, b), default=0.0)
    for i in range(len(a)):
        for j in range(i + 1, len(a)):
            da, db = _sign(a[i] - a[j], scale_a), _sign(b[i] - b[j], scale_b)
            if da == 0 and db == 0:
                continue
            pairs += 1
            concordant += 1 if da == db else 0
    return None if pairs == 0 else concordant / pairs


def compare(forecast: ForecastResult, actuals: list[Actual]) -> dict:
    if not forecast.available:
        return {"available": False, "reasons": [r.value for r in forecast.reasons]}
    rows, zero_actual = [], next(a for a in actuals if a.budget_rps == 0)
    zero_pred = forecast.budget(0).band(Band.CENTRAL)
    direction = {"queue": [], "new_ambiguous": [], "resolved_by_check": []}
    for a in actuals:
        fb = forecast.budget(a.budget_rps)
        c, p, o = fb.band(Band.CENTRAL), fb.band(Band.PESSIMISTIC), fb.band(Band.OPTIMISTIC)
        row = {
            "budget_rps": a.budget_rps,
            "pred_queue": c.queue_mean,
            "act_queue": a.queue_mean,
            "queue_in_band": o.queue_mean - 1e-9 <= a.queue_mean <= p.queue_mean + 1e-9,
            "pred_new_ambiguous": c.new_ambiguous,
            "act_new_ambiguous": a.new_ambiguous,
            "amb_in_band": o.new_ambiguous - DIRECTION_TOLERANCE
            <= a.new_ambiguous
            <= p.new_ambiguous + DIRECTION_TOLERANCE,
            "pred_backlog": c.backlog_end,
            "act_backlog": a.backlog_end,
            "pred_checks": c.checks_released,
            "act_checks": a.checks_released,
            "pred_on_time": c.checks_on_time,
            "act_on_time": a.checks_on_time,
            "pred_resolved_by_check": c.expected_authoritative,
            "act_resolved_by_check": a.resolved_by_check,
            "pred_wait_s": c.mean_payment_wait_s,
            "act_wait_littles_s": a.mean_wait_littles_s,
            "act_answered_latency_s": a.answered_payment_latency_s,
            "pred_incremental_ambiguity": fb.incremental_ambiguity,
            "act_incremental_ambiguity": a.new_ambiguous - zero_actual.new_ambiguous,
        }
        rows.append(row)
        if a.budget_rps:
            for key, pv, av in (
                (
                    "queue",
                    c.queue_mean - zero_pred.queue_mean,
                    a.queue_mean - zero_actual.queue_mean,
                ),
                (
                    "new_ambiguous",
                    c.new_ambiguous - zero_pred.new_ambiguous,
                    a.new_ambiguous - zero_actual.new_ambiguous,
                ),
                ("resolved_by_check", c.expected_authoritative, a.resolved_by_check),
            ):
                scale = {
                    "queue": zero_actual.queue_mean,
                    "new_ambiguous": zero_actual.new_ambiguous,
                }.get(key, 0.0)
                direction[key].append(_sign(pv, scale) == _sign(av, scale))
    pred_amb = [forecast.budget(a.budget_rps).band(Band.CENTRAL).new_ambiguous for a in actuals]
    act_amb = [a.new_ambiguous for a in actuals]
    return {
        "available": True,
        "reasons": [r.value for r in forecast.reasons],
        "rows": rows,
        "direction_agreement": {k: (sum(v) / len(v) if v else None) for k, v in direction.items()},
        "rank_agreement_new_ambiguous": kendall(pred_amb, act_amb),
        "ranking_reversed": (kendall(pred_amb, act_amb) or 1.0) < 0.5,
    }


@dataclass(frozen=True)
class CaseRecord:
    case: str
    description: str
    seed: int
    route_id: str
    decision_s: float
    engine: str
    model_selection: str
    estimator_mode: str
    estimator_identifiability: str
    capacity_lower_bound_rps: float | None
    true_capacity_rps: float
    eligible_now: int
    eligible_in_horizon: int
    check_benefit_low: float
    check_benefit_identified: bool
    probes_answered: int
    full: dict
    no_feedback: dict
    no_repay: dict
    forecast_ms: float
    result_hash: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, default=str)


def evaluate_case(case: LookaheadCase, config: LookaheadConfig | None = None) -> CaseRecord:
    config = config or LookaheadConfig(horizon_s=HORIZON_S)
    workload = generate_workload(case.config)
    schedule = probe_schedule(case) + tuple(
        CheckRelease(case.decision_s, case.route_id, b) for b in case.budgets if b
    )
    selection = select_model(case.config, schedule, Purpose.PRIMARY)
    observed = observe(case, workload)
    policy = case.config.timing_policy
    began = time.perf_counter()
    full = forecast_budgets(observed.forecast_input, case.budgets, policy, config)
    elapsed_ms = (time.perf_counter() - began) * 1000.0
    ablations = {
        mode: forecast_budgets(
            observed.forecast_input,
            case.budgets,
            policy,
            config.model_copy(update={"feedback_mode": mode}),
        )
        for mode in (FeedbackMode.NO_FEEDBACK, FeedbackMode.NO_REPAY)
    }
    actuals = [actual_outcome(case, workload, b) for b in case.budgets]
    cap = observed.forecast_input.capacity
    truth_cap = ce.true_capacity(
        workload, case.route_id, case.decision_s, case.decision_s + HORIZON_S
    )
    body = {
        "full": compare(full, actuals),
        "no_feedback": compare(ablations[FeedbackMode.NO_FEEDBACK], actuals),
        "no_repay": compare(ablations[FeedbackMode.NO_REPAY], actuals),
    }
    digest = hashlib.sha256(
        json.dumps(
            {"case": case.name, "seed": case.config.seed, "forecast": full.digest(), "body": body},
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()
    return CaseRecord(
        case=case.name,
        description=case.description,
        seed=case.config.seed,
        route_id=case.route_id,
        decision_s=case.decision_s,
        engine="DES",
        model_selection=f"{selection.model.value}/{selection.status.value}: {selection.reason}",
        estimator_mode=cap.mode.value,
        estimator_identifiability=cap.identifiability.value,
        capacity_lower_bound_rps=cap.capacity_lower_bound_rps,
        true_capacity_rps=truth_cap,
        eligible_now=full.eligible_now,
        eligible_in_horizon=full.eligible_in_horizon,
        check_benefit_low=full.check_benefit_low,
        check_benefit_identified=full.check_benefit_identified,
        probes_answered=observed.probes_answered,
        full=body["full"],
        no_feedback=body["no_feedback"],
        no_repay=body["no_repay"],
        forecast_ms=elapsed_ms,
        result_hash=digest,
    )


# ----------------------------------------------------------------- evidence generator

SOURCE_FILES = (
    "intelligence/controller/lookahead.py",
    "intelligence/experiments/lookahead_evaluation.py",
    "intelligence/ml/capacity_estimator.py",
    "intelligence/experiments/capacity_evaluation.py",
    "intelligence/simulation/des_validate.py",
    "intelligence/simulation/model_selection.py",
    "intelligence/data/generate_dataset.py",
    "intelligence/simulation/scenarios.py",
)


def source_digest() -> str:
    root = Path(__file__).resolve().parents[2]
    h = hashlib.sha256()
    for rel in SOURCE_FILES:
        h.update(rel.encode())
        h.update((root / rel).read_bytes())
    return h.hexdigest()


def main() -> None:  # pragma: no cover - evidence generator
    out = Path("results/processed/stage07")
    out.mkdir(parents=True, exist_ok=True)
    began = time.perf_counter()
    records = []
    with (out / "lookahead_runs.jsonl").open("w", encoding="utf-8") as sink:
        for seed in SEEDS:
            for case in cases(seed):
                record = evaluate_case(case)
                sink.write(record.to_json() + "\n")
                records.append(record)
                print(case.name, seed, record.result_hash[:12], flush=True)
    manifest = {
        "label": "Stage 07 development smoke results (not held-out evidence)",
        "seeds": SEEDS,
        "rate_scale": SCALE,
        "horizon_s": HORIZON_S,
        "lookahead_config": json.loads(LookaheadConfig(horizon_s=HORIZON_S).model_dump_json()),
        "source_digest": source_digest(),
        "elapsed_s": round(time.perf_counter() - began, 1),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    (out / "lookahead_summary.md").write_text(render(records, manifest))
    files = sorted(p for p in out.iterdir() if p.name != "SHA256SUMS")
    (out / "SHA256SUMS").write_text(
        "".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in files)
    )


def render(records, manifest) -> str:
    def f(x, d=2):
        if x is None:
            return "-"
        if isinstance(x, bool):
            return "yes" if x else "no"
        return f"{x:.{d}f}" if isinstance(x, float) else str(x)

    lines = [
        "# Stage 07 look-ahead vs matched DES: development smoke results",
        "",
        f"Not held-out evidence. Source digest {manifest['source_digest'][:12]}, horizon "
        f"{manifest['horizon_s']} s, scale {manifest['rate_scale']}.",
        "",
        "## Case summary (FULL forecast)",
        "",
        "| case | seed | est. mode | ident | L | true C | elig now | benefit low (id) "
        "| dir queue | dir amb | dir checks | rank amb | NO_FEEDBACK dir amb | ms |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in records:
        full, nf = r.full, r.no_feedback
        if not full["available"]:
            lines.append(
                f"| {r.case} | {r.seed} | {r.estimator_mode} | "
                f"{r.estimator_identifiability} | {f(r.capacity_lower_bound_rps)} | "
                f"{f(r.true_capacity_rps)} | - | - | forecast unavailable: "
                f"{', '.join(full['reasons'])} | | | | | {f(r.forecast_ms)} |"
            )
            continue
        d, dn = full["direction_agreement"], nf["direction_agreement"]
        lines.append(
            f"| {r.case} | {r.seed} | {r.estimator_mode} | {r.estimator_identifiability} "
            f"| {f(r.capacity_lower_bound_rps)} | {f(r.true_capacity_rps)} | {r.eligible_now} "
            f"| {f(r.check_benefit_low)} ({f(r.check_benefit_identified)}) | {f(d['queue'])} "
            f"| {f(d['new_ambiguous'])} | {f(d['resolved_by_check'])} "
            f"| {f(full['rank_agreement_new_ambiguous'])} | {f(dn['new_ambiguous'])} "
            f"| {f(r.forecast_ms)} |"
        )
    lines += [
        "",
        "## Per budget (FULL CENTRAL forecast vs actual)",
        "",
        "| case | seed | b | queue pred/act (band?) | new amb pred/act (band?) "
        "| incr amb pred/act | checks pred/act | on-time pred/act | by check pred/act "
        "| backlog pred/act | wait pred/act s |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in records:
        if not r.full["available"]:
            continue
        for row in r.full["rows"]:
            lines.append(
                f"| {r.case} | {r.seed} | {row['budget_rps']} "
                f"| {f(row['pred_queue'], 1)}/{f(row['act_queue'], 1)} "
                f"({f(row['queue_in_band'])}) "
                f"| {f(row['pred_new_ambiguous'], 1)}/{f(row['act_new_ambiguous'], 1)} "
                f"({f(row['amb_in_band'])}) "
                f"| {f(row['pred_incremental_ambiguity'], 1)}/"
                f"{f(row['act_incremental_ambiguity'], 1)} "
                f"| {f(row['pred_checks'], 1)}/{f(row['act_checks'], 1)} "
                f"| {f(row['pred_on_time'], 1)}/{f(row['act_on_time'], 1)} "
                f"| {f(row['pred_resolved_by_check'], 1)}/{f(row['act_resolved_by_check'], 1)} "
                f"| {f(row['pred_backlog'], 1)}/{f(row['act_backlog'], 1)} "
                f"| {f(row['pred_wait_s'])}/{f(row['act_wait_littles_s'])} |"
            )
    lines += ["", f"elapsed {manifest['elapsed_s']} s", ""]
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover
    main()
