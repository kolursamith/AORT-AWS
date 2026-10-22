"""Capacity-estimator evaluation with simulated ground truth (Stage 06).

Separation (enforced by structure and tests)::

    simulation truth (workload.ground_truth, engine truth intervals) -> score_run ONLY
    remitter-observable records -> RouteTelemetry -> Track B-style envelope -> Route Twin
        -> RouteTwinSnapshot -> CapacityEstimator

* ``remitter_view`` projects an engine run onto what a remitter observes about its own traffic:
  route, request kind, send time, response receipt time. ``des_telemetry`` builds windowed
  ``RouteTelemetry`` from that projection only. The fluid engine already emits own-traffic
  ``RouteTelemetry`` in its ``ControllerObservation``.
* Telemetry impairments (staleness, missing windows, poisoning) are applied to the observable
  stream, never to the simulation, so S1/S2/S3 stay unchanged.
* The estimator receives nothing but Route Twin snapshots and the decision time.
* ``score_run`` is the only place that reads ground truth (capacity schedule, true arrivals).

The engine for every design comes from the Stage 04S selector (``select_model``, PRIMARY, no
check schedule). Passive estimator designs carry no check bursts, so they fall outside the
frozen validation table and the selector chooses the DES; the fluid path exists for designs
the table does permit.

Metric definitions (truth ``C`` = time-average of the true capacity over the decision's window,
``D`` = true arrivals per second in that window; all per decision, one decision per window):

* point errors (MAE, RMSE, median AE, mean relative error) over decisions with a point estimate;
* unsafe point overestimate: ``C_hat > 1.05 C``; bound coverage: ``L <= 1.01 C`` (1 % allows for
  response lag inside one window); headroom overestimate: ``H > 0`` and ``D10 + H > C`` with
  ``D10`` the true arrival rate averaged over the last 10 s (one-window arrivals are Poisson
  noise, not load);
* congestion detection delay: first CONGESTED decision at or after the fault start, minus the
  fault start; recovery acknowledgement delay: first HEALTHY decision at or after the fault
  end, minus the fault end; recovered-capacity detection delay: first decision after the fault
  end with ``L >= 1.1 x`` the degraded capacity;
* periods: A before degradation, B the first 60 s of it, C the rest of it, D 300 s after it
  ends, E decisions with poisoned telemetry, F decisions in INITIALIZING / STALE /
  INSUFFICIENT_DATA / FAIL_CLOSED mode or with stale or missing telemetry;
* identification check: decisions claiming IDENTIFIED although the true bottleneck was never
  provably busy (at least two requests at two consecutive window ends) during the estimator's
  point horizon (the last 120 s), so no saturated sample could justify a point estimate;
* isolation errors: estimate digests that differ when a route is replayed alone.

All results here are development smoke results, not held-out evidence.
"""

import hashlib
import json
import math
import time
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from contracts.v1_1 import SCHEMA_VERSION, RouteTelemetry, ValidatedRouteTelemetry
from intelligence.common.clock import ManualClock
from intelligence.data.generate_dataset import Workload, generate_workload
from intelligence.experiments.metrics import canonical_json, nearest_rank, sha256_json
from intelligence.ml.capacity_estimator import (
    ESTIMATOR_VERSION,
    CapacityEstimate,
    CapacityEstimator,
    CapacityEstimatorConfig,
    EstimatorMode,
    RouteState,
)
from intelligence.simulation.model_selection import (
    VALIDATION_TABLE,
    Model,
    ModelSelection,
    Purpose,
    select_model,
)
from intelligence.simulation.scenarios import (
    FaultSpec,
    RouteSpec,
    ScenarioConfig,
    ScenarioOptions,
    reference_scenario,
    research_timing_policy,
)
from intelligence.twin.route_twin import RouteTwinConfig, RouteTwinRegistry

SMOKE_SEEDS = (6001, 6002, 6003)
SMOKE_RATE_SCALE = 0.05
SMOKE_DURATION_S = 2400.0
WINDOW_S = 1.0
POINT_OVER_TOLERANCE = 0.05
BOUND_TOLERANCE = 0.01
B_PERIOD_S = 60.0
POINT_HORIZON_S = 120.0  # matches CapacityEstimatorConfig.point_horizon_s
D_PERIOD_S = 300.0
_INSUFFICIENT_MODES = frozenset(
    {
        EstimatorMode.INITIALIZING,
        EstimatorMode.STALE,
        EstimatorMode.INSUFFICIENT_DATA,
        EstimatorMode.FAIL_CLOSED,
    }
)

# ----------------------------------------------------------------- observable projection


@dataclass(frozen=True, slots=True)
class RemitterRecord:
    """What a remitter knows about one of its own requests. No outcome, no capacity."""

    route_id: str
    kind: str
    sent_s: float
    response_s: float | None  # receipt time at the remitter; None if never received


def remitter_view(result) -> tuple[RemitterRecord, ...]:
    """Project a DES result onto remitter-observable fields (send and receipt times only)."""

    return tuple(RemitterRecord(t.route_id, t.kind, t.sent_s, t.response_s) for t in result.traces)


def des_telemetry(
    records: Sequence[RemitterRecord],
    *,
    experiment_id: str,
    routes: Sequence[str],
    epoch: datetime,
    horizon_s: float,
    client_timeout_s: float,
    window_s: float = WINDOW_S,
) -> dict[str, list[RouteTelemetry]]:
    """Own-traffic telemetry per route and window, with the fluid engine's field semantics.

    payment_rps: sends; observed_throughput_rps: responses received (timely or late);
    latency: receipt minus send; timeout: client timeout passed with no response yet;
    queue_depth: sent, not yet answered within the timeout and not yet timed out.
    """

    if any(r.kind not in ("payment", "repayment") for r in records):
        raise ValueError("des_telemetry covers payments and re-payments only")
    n = int(math.floor(horizon_s / window_s + 1e-9))
    acc = {
        route: {
            "sends": [0] * n,
            "responses": [0] * n,
            "timeouts": [0] * n,
            "depth": [0] * (n + 1),
            "lat": [[] for _ in range(n)],
        }
        for route in routes
    }

    def idx(t: float) -> int | None:
        k = int(math.floor(t / window_s + 1e-12))
        return k if 0 <= k < n else None

    for r in records:
        a = acc.get(r.route_id)
        if a is None:
            continue
        deadline = r.sent_s + client_timeout_s
        if (k := idx(r.sent_s)) is not None:
            a["sends"][k] += 1
        if r.response_s is not None and (k := idx(r.response_s)) is not None:
            a["responses"][k] += 1
            a["lat"][k].append((r.response_s - r.sent_s) * 1000.0)
        timely = r.response_s is not None and r.response_s <= deadline
        if not timely and (k := idx(deadline)) is not None:
            a["timeouts"][k] += 1
        end_await = r.response_s if timely else deadline
        first = int(math.floor(r.sent_s / window_s + 1e-12))
        last = int(math.floor(end_await / window_s + 1e-12)) - 1
        if first < n and last >= first:
            a["depth"][first] += 1
            a["depth"][min(last + 1, n)] -= 1

    out: dict[str, list[RouteTelemetry]] = {}
    for route, a in acc.items():
        series, running = [], 0
        for k in range(n):
            running += a["depth"][k]
            lat = sorted(a["lat"][k])
            responses, timeouts = a["responses"][k], a["timeouts"][k]
            determined = responses + timeouts
            start = epoch + timedelta(seconds=k * window_s)
            end = epoch + timedelta(seconds=(k + 1) * window_s)
            series.append(
                RouteTelemetry(
                    schema_version=SCHEMA_VERSION,
                    experiment_id=experiment_id,
                    route_id=route,
                    source_id=f"sim-remitter-{route}",
                    observation_scope="REMITTER_OWN_TRAFFIC",
                    sequence=k,
                    observation_window_start=start,
                    observation_window_end=end,
                    observation_timestamp=end,
                    payment_rps=a["sends"][k] / window_s,
                    check_rps=0.0,
                    observed_throughput_rps=responses / window_s,
                    completed_payment_rps=responses / window_s,
                    completed_check_rps=0.0,
                    p50_latency_ms=nearest_rank(lat, 0.5),
                    p95_latency_ms=nearest_rank(lat, 0.95),
                    latency_sample_count=len(lat),
                    timeout_rate=timeouts / determined if determined else None,
                    timeout_count=timeouts,
                    timeout_denominator_count=determined,
                    queue_depth=running,
                )
            )
        out[route] = series
    return out


# ----------------------------------------------------------------- telemetry impairments


@dataclass(frozen=True)
class Impairment:
    """Changes to the observable stream of one route (never to the simulation)."""

    route_id: str
    freeze: tuple[float, float] | None = None  # no telemetry delivered in [start, end)
    missing_fraction: float = 0.0  # deterministic per window
    inflate_from_s: float | None = None
    inflate_factor: float = 1.0
    drop_queue_and_latency: bool = False  # a poorer source: no queue depth or latency

    def poisoned(self, t: float) -> bool:
        return self.inflate_from_s is not None and t >= self.inflate_from_s

    def missing(self, seq: int, t: float) -> bool:
        if self.freeze is not None and self.freeze[0] <= t < self.freeze[1]:
            return True
        if self.missing_fraction <= 0:
            return False
        h = hashlib.sha256(f"missing|{self.route_id}|{seq}".encode()).digest()
        return int.from_bytes(h[:8], "big") / 2**64 < self.missing_fraction


def inflate_throughput(obs: RouteTelemetry, factor: float) -> RouteTelemetry:
    """Attack model: completions reported ``factor`` times higher, kept contract-valid."""

    data = obs.model_dump()
    for name in ("observed_throughput_rps", "completed_payment_rps", "completed_check_rps"):
        if data[name] is not None:
            data[name] = data[name] * factor
    return RouteTelemetry.model_validate(data)


def degrade_fields(obs: RouteTelemetry) -> RouteTelemetry:
    data = obs.model_dump()
    data.update(queue_depth=None, p50_latency_ms=None, p95_latency_ms=None,
                latency_sample_count=0)  # fmt: skip
    return RouteTelemetry.model_validate(data)


# ----------------------------------------------------------------- designs


@dataclass(frozen=True)
class Design:
    name: str
    description: str
    config: ScenarioConfig
    routes: tuple[str, ...]
    impairments: tuple[Impairment, ...] = ()
    label: str = "development smoke"


def _custom(name, seed, routes, faults=(), *, scale=SMOKE_RATE_SCALE, duration=SMOKE_DURATION_S):
    return ScenarioConfig(
        scenario_id=f"cap-{name}",
        description=f"Stage 06 estimator design {name}",
        reference="Stage 06 development smoke designs",
        experiment_id="EXP-S06",
        seed=seed,
        duration_s=duration,
        routes=tuple(
            RouteSpec(
                route_id=rid,
                remitter_bank_id="A1",
                beneficiary_bank_id=bank,
                payment_rps=rho * 1000.0 * scale * share,
                capacity_rps=1000.0 * scale * share,
            )
            for rid, bank, rho, share in routes
        ),
        faults=tuple(faults),
        timing_policy=research_timing_policy(),
    )


def _cut(route, start, end, magnitude, kind="CAPACITY_CUT"):
    return FaultSpec(fault_id=f"{kind.lower()}-{route}-{int(start)}", route_id=route, kind=kind,
                     start_s=start, end_s=end, magnitude=magnitude)  # fmt: skip


def designs(seed: int, scale: float = SMOKE_RATE_SCALE, duration: float = SMOKE_DURATION_S):
    """The fifteen required designs plus two poisoning variants. S1/S2/S3 are unchanged."""

    def ref(name, **options):
        return reference_scenario(
            name,
            seed=seed,
            experiment_id="EXP-S06",
            rate_scale=scale,
            duration_s=duration,
            options=ScenarioOptions(**options),
        )

    ab = ("A1-B",)
    one = (("A1-B", "B", 0.7, 1.0),)
    out = [
        Design("normal_underutilized", "rho 0.3, no fault",
               _custom("under", seed, (("A1-B", "B", 0.3, 1.0),), scale=scale,
                       duration=duration), ab),
        Design("moderate_load", "S1 (rho 0.7)", ref("S1"), ab),
        Design("near_saturation", "rho 0.95, no fault",
               _custom("near", seed, (("A1-B", "B", 0.95, 1.0),), scale=scale,
                       duration=duration), ab),
        Design("severe_overload", "rho 0.7, capacity -60 % for 600 s (rho 1.75)",
               _custom("severe", seed, one, (_cut("A1-B", 600.0, 1200.0, 0.6),), scale=scale,
                       duration=duration), ab),
        Design("sudden_capacity_drop", "rho 0.7, capacity -50 % from 600 s to the end",
               _custom("drop", seed, one, (_cut("A1-B", 600.0, duration, 0.5),), scale=scale,
                       duration=duration), ab),
        Design("prolonged_degradation", "S2", ref("S2"), ab),
        Design("fault_recovery", "S3 (2 min fault, then recovery)", ref("S3"), ab),
        Design("oscillating_capacity", "S2 with 30 % dips every 300 s after the fault",
               ref("S2", flapping=True), ab),
        Design("zero_throughput", "rho 0.7, capacity 0 from 600 to 900 s",
               _custom("zero", seed, one, (_cut("A1-B", 600.0, 900.0, 1.0),), scale=scale,
                       duration=duration), ab),
        Design("payment_burst", "S1 with 3x bursts for 30 s every 300 s",
               ref("S1", bursty=True), ab),
        Design("stale_telemetry", "S2; no telemetry delivered 900-1100 s", ref("S2"), ab,
               (Impairment("A1-B", freeze=(900.0, 1100.0)),)),
        Design("missing_telemetry", "S2; 30 % of windows missing", ref("S2"), ab,
               (Impairment("A1-B", missing_fraction=0.3),)),
        Design("subtle_poisoning", "S1; throughput reported +30 % from 1200 s", ref("S1"), ab,
               (Impairment("A1-B", inflate_from_s=1200.0, inflate_factor=1.3),)),
        Design("gross_poisoning", "S2; throughput reported +100 % from 300 s", ref("S2"), ab,
               (Impairment("A1-B", inflate_from_s=300.0, inflate_factor=2.0),)),
        Design("poisoning_during_congestion", "S2; throughput reported +30 % from 900 s",
               ref("S2"), ab,
               (Impairment("A1-B", inflate_from_s=900.0, inflate_factor=1.3),)),
        Design("healthy_parallel_route", "S2 plus an unfaulted A1-C",
               ref("S2", healthy_parallel_route=True), ("A1-B", "A1-C")),
        Design("two_routes_quality", "A1-B C=50 good telemetry; A1-C C=20 faulted, poor source",
               _custom("quality", seed, (("A1-B", "B", 0.7, 1.0), ("A1-C", "C", 0.5, 0.4)),
                       (_cut("A1-C", 600.0, 1800.0, 0.5),), scale=scale, duration=duration),
               ("A1-B", "A1-C"),
               (Impairment("A1-C", missing_fraction=0.2, drop_queue_and_latency=True),)),
    ]  # fmt: skip
    return out


# ----------------------------------------------------------------- engine and twin feed


def choose_engine(config: ScenarioConfig, table=VALIDATION_TABLE) -> ModelSelection:
    """Stage 04S rule: passive observation (no checks), PRIMARY purpose."""

    return select_model(config, (), Purpose.PRIMARY, table=table)


@dataclass
class ObservedRun:
    """Observable telemetry plus truth, kept apart: estimator code never receives ``truth``."""

    telemetry: dict[str, list[RouteTelemetry]]
    truth_offered: dict[str, list[float]]  # true arrivals per window (scorer only)
    truth_depth: dict[str, list[int]]  # true bottleneck depth at each window end (scorer only)
    engine: str
    dt_s: float


def run_engine(workload: Workload, selection: ModelSelection, routes: Sequence[str]) -> ObservedRun:
    config = workload.manifest.config
    if selection.model is Model.DES:
        from intelligence.simulation.des_validate import DesConfig, simulate_des

        result = simulate_des(workload, DesConfig(bin_s=WINDOW_S))
        telemetry = des_telemetry(
            remitter_view(result),
            experiment_id=config.experiment_id,
            routes=routes,
            epoch=config.epoch,
            horizon_s=result.horizon_s,
            client_timeout_s=config.timing_policy.client_timeout_s,
        )
        truth, depth = defaultdict(list), defaultdict(list)
        for i in result.intervals:
            if i.route_id in routes:
                truth[i.route_id].append(
                    (i.payment_arrivals + i.repayment_arrivals + i.resubmission_arrivals
                     + i.checks_released + i.background_check_arrivals) / WINDOW_S
                )  # fmt: skip
                depth[i.route_id].append(i.bottleneck_queue_depth)
        return ObservedRun(telemetry, dict(truth), dict(depth), "DES", WINDOW_S)

    from intelligence.simulation.engine import FluidSimulation, SimulationConfig

    dt = selection.dt_s
    sim = FluidSimulation(workload, SimulationConfig(dt_s=dt))
    telemetry = defaultdict(list)
    while not sim.done:
        obs = sim.step(())
        for t in obs.telemetry:
            if t.route_id in routes:
                telemetry[t.route_id].append(t)
    truth, depth = defaultdict(list), defaultdict(list)
    for m in sim.result().metrics:
        if m.route_id in routes:
            truth[m.route_id].append(
                (m.payment_arrivals + m.repayment_arrivals + m.resubmission_arrivals
                 + m.checks_released + m.background_check_arrivals) / dt
            )  # fmt: skip
            depth[m.route_id].append(m.bottleneck_queue_depth)
    return ObservedRun(dict(telemetry), dict(truth), dict(depth), "FLUID", dt)


@dataclass
class Decision:
    t_s: float  # window end, seconds from the epoch
    route_id: str
    estimate: CapacityEstimate
    delivered: bool
    poisoned: bool
    compute_s: float


def twin_config() -> RouteTwinConfig:
    return RouteTwinConfig(model_version="route-twin-1", max_observation_age_s=30.0,
                           max_future_skew_s=1.0, freshness_threshold_s=5.0)  # fmt: skip


def feed_estimators(
    config: ScenarioConfig,
    telemetry: dict[str, list[RouteTelemetry]],
    routes: Sequence[str],
    impairments: Sequence[Impairment] = (),
    estimator_config: CapacityEstimatorConfig | None = None,
    keep_snapshots: bool = False,
):
    """Window by window: deliver telemetry to the Route Twin, snapshot, estimate (per route).

    Telemetry is delivered at its window end as an AUTHENTICATED, unflagged envelope (the
    worst case for poisoning: Track B's validator did not flag it). The decision for every
    route happens at each window end, delivered or not.
    """

    clock = ManualClock(config.epoch)
    twin = RouteTwinRegistry(twin_config(), clock)
    for route in routes:
        twin.register_route(config.route(route).descriptor(config.experiment_id),
                            [f"sim-remitter-{route}"])  # fmt: skip
    estimators = {r: CapacityEstimator(config.experiment_id, r, estimator_config) for r in routes}
    impaired = {i.route_id: i for i in impairments}
    decisions: list[Decision] = []
    snapshots: dict[str, list] = defaultdict(list)
    n = min(len(telemetry[r]) for r in routes)
    for k in range(n):
        end = telemetry[routes[0]][k].observation_window_end
        clock.set_time(end)
        t_s = (end - config.epoch).total_seconds()
        delivered = {}
        for route in routes:
            obs = telemetry[route][k]
            imp = impaired.get(route)
            poisoned = False
            if imp is not None:
                if imp.missing(obs.sequence, t_s - WINDOW_S):
                    delivered[route] = (False, False)
                    continue
                if imp.poisoned(t_s):
                    obs, poisoned = inflate_throughput(obs, imp.inflate_factor), True
                if imp.drop_queue_and_latency:
                    obs = degrade_fields(obs)
            twin.ingest(_envelope(obs))
            delivered[route] = (True, poisoned)
        for route in routes:
            snap = twin.snapshot(config.experiment_id, route)
            began = time.perf_counter()
            estimate = estimators[route].update(snap, end)
            spent = time.perf_counter() - began
            if keep_snapshots:
                snapshots[route].append((snap, end))
            ok, poisoned = delivered[route]
            decisions.append(Decision(t_s, route, estimate, ok, poisoned, spent))
    return decisions, snapshots


def _envelope(obs: RouteTelemetry) -> ValidatedRouteTelemetry:
    return ValidatedRouteTelemetry(
        schema_version=SCHEMA_VERSION,
        experiment_id=obs.experiment_id,
        route_id=obs.route_id,
        telemetry=obs,
        trust_status="AUTHENTICATED",
        validator_id="eval-validator",
        validator_version="eval-1",
        validated_at=obs.observation_timestamp,
        cross_check_status="UNAVAILABLE",
    )


def isolation_errors(snapshots, decisions, estimator_config=None) -> int:
    """Replay each route alone through a fresh estimator; count digest mismatches."""

    original = defaultdict(list)
    for d in decisions:
        original[d.route_id].append(d.estimate.digest())
    errors = 0
    for route, items in snapshots.items():
        alone = CapacityEstimator(items[0][0].experiment_id, route, estimator_config)
        replay = [alone.update(snap, at).digest() for snap, at in items]
        errors += sum(1 for a, b in zip(replay, original[route], strict=True) if a != b)
    return errors


# ----------------------------------------------------------------- truth and scoring


def true_capacity(workload: Workload, route_id: str, start_s: float, end_s: float) -> float:
    """Time-average of the hidden capacity schedule over [start, end) (scorer only)."""

    changes = sorted(
        (c.offset_s, c.capacity_rps)
        for c in workload.ground_truth.capacity
        if c.route_id == route_id
    )
    total, cursor = 0.0, start_s
    current = 0.0
    for offset, value in changes:
        if offset <= start_s:
            current = value
            continue
        if offset >= end_s:
            break
        total += current * (offset - cursor)
        cursor, current = offset, value
    total += current * (end_s - cursor)
    return total / (end_s - start_s)


def degradation_window(config: ScenarioConfig, route_id: str) -> tuple[float, float] | None:
    cuts = [f for f in config.faults if f.route_id == route_id and f.kind == "CAPACITY_CUT"]
    if not cuts:
        return None
    first = min(cuts, key=lambda f: f.start_s)
    return first.start_s, min(first.end_s, config.duration_s)


@dataclass
class PeriodScore:
    decisions: int = 0
    point_decisions: int = 0
    abs_errors: list = field(default_factory=list)
    sq_errors: list = field(default_factory=list)
    rel_errors: list = field(default_factory=list)
    unsafe_point: int = 0
    bound_decisions: int = 0
    bound_violations: int = 0
    headroom_positive: int = 0
    headroom_over: int = 0
    insufficient: int = 0
    false_identified: int = 0

    def summary(self) -> dict:
        def frac(a, b):
            return None if not b else a / b

        mae = sum(self.abs_errors) / len(self.abs_errors) if self.abs_errors else None
        rmse = math.sqrt(sum(self.sq_errors) / len(self.sq_errors)) if self.sq_errors else None
        return {
            "decisions": self.decisions,
            "point_decisions": self.point_decisions,
            "mae_rps": mae,
            "rmse_rps": rmse,
            "median_ae_rps": nearest_rank(self.abs_errors, 0.5),
            "mean_rel_error": (
                sum(self.rel_errors) / len(self.rel_errors) if self.rel_errors else None
            ),
            "unsafe_point_rate": frac(self.unsafe_point, self.point_decisions),
            "bound_decisions": self.bound_decisions,
            "bound_coverage": frac(
                self.bound_decisions - self.bound_violations, self.bound_decisions
            ),  # fmt: skip
            "bound_violations": self.bound_violations,
            "headroom_positive": self.headroom_positive,
            "headroom_overestimate_rate": frac(self.headroom_over, self.decisions),
            "headroom_overestimates": self.headroom_over,
            "insufficient_fraction": frac(self.insufficient, self.decisions),
            "false_identified": self.false_identified,
        }


def score_run(
    workload: Workload,
    design: Design,
    decisions: Sequence[Decision],
    truth_offered: dict[str, list[float]],
    truth_depth: dict[str, list[int]],
    dt_s: float = WINDOW_S,
) -> dict:
    """Score decisions against hidden truth. The only function that reads ground truth."""

    config = design.config
    impaired = {i.route_id: i for i in design.impairments}
    out = {}
    for route in design.routes:
        window = degradation_window(config, route)
        periods = defaultdict(PeriodScore)
        series = [d for d in decisions if d.route_id == route]
        detect = restore = recover = recovered_bound = None
        degraded_capacity = last_busy = None
        if window is not None:
            degraded_capacity = true_capacity(workload, route, window[0], window[1])
        for k, d in enumerate(series):
            e = d.estimate
            start = d.t_s - WINDOW_S
            c = true_capacity(workload, route, start, d.t_s)
            span = max(1, int(round(10.0 / dt_s)))
            recent = truth_offered[route][max(0, k + 1 - span) : k + 1]
            offered = sum(recent) / len(recent) if recent else 0.0
            busy_now = k > 0 and min(truth_depth[route][k - 1 : k + 1]) >= 2
            last_busy = k if busy_now else last_busy
            busy = last_busy is not None and (k - last_busy) * dt_s <= POINT_HORIZON_S
            names = []
            if window is None or d.t_s <= window[0]:
                names.append("A_before")
            elif d.t_s <= window[0] + B_PERIOD_S:
                names.append("B_onset")
            elif d.t_s <= window[1]:
                names.append("C_sustained")
            elif d.t_s <= window[1] + D_PERIOD_S:
                names.append("D_after_recovery")
            if d.poisoned:
                names.append("E_poisoned")
            imp = impaired.get(route)
            impaired_now = not d.delivered or (imp is not None and imp.drop_queue_and_latency)
            if e.mode in _INSUFFICIENT_MODES or impaired_now:
                names.append("F_insufficient_observation")
            names.append("all")
            for name in names:
                p = periods[name]
                p.decisions += 1
                if e.mode in _INSUFFICIENT_MODES:
                    p.insufficient += 1
                if e.estimated_capacity_rps is not None:
                    err = e.estimated_capacity_rps - c
                    p.point_decisions += 1
                    p.abs_errors.append(abs(err))
                    p.sq_errors.append(err * err)
                    if c > 0:
                        p.rel_errors.append(abs(err) / c)
                    if e.estimated_capacity_rps > (1 + POINT_OVER_TOLERANCE) * c:
                        p.unsafe_point += 1
                    if not busy:
                        p.false_identified += 1
                if e.capacity_lower_bound_rps is not None:
                    p.bound_decisions += 1
                    if e.capacity_lower_bound_rps > (1 + BOUND_TOLERANCE) * c + 1e-9:
                        p.bound_violations += 1
                if e.safe_headroom_rps > 0:
                    p.headroom_positive += 1
                    if offered + e.safe_headroom_rps > c:
                        p.headroom_over += 1
            if window is not None:
                inside = d.t_s - WINDOW_S >= window[0] - 1e-9
                if detect is None and inside and e.route_state is RouteState.CONGESTED:
                    detect = d.t_s - window[0]
                lb = e.capacity_lower_bound_rps
                if (
                    restore is None
                    and inside
                    and lb is not None
                    and (lb <= (1 + BOUND_TOLERANCE) * c)
                ):
                    restore = d.t_s - window[0]
                if window[1] < config.duration_s and d.t_s - WINDOW_S >= window[1] - 1e-9:
                    if recover is None and e.route_state is RouteState.HEALTHY:
                        recover = d.t_s - window[1]
                    if (
                        recovered_bound is None
                        and lb is not None
                        and (lb >= 1.1 * degraded_capacity)
                    ):
                        recovered_bound = d.t_s - window[1]
        compute = sorted(d.compute_s for d in series)
        out[route] = {
            "degradation_window_s": window,
            "true_capacity_degraded_rps": degraded_capacity,
            "congestion_detection_delay_s": detect,
            "bound_restoration_delay_s": restore,
            "recovery_ack_delay_s": recover,
            "recovered_capacity_detection_delay_s": recovered_bound,
            "periods": {name: p.summary() for name, p in sorted(periods.items())},
            "modes": dict(sorted(_count(e.estimate.mode.value for e in series).items())),
            "identifiability": dict(
                sorted(_count(e.estimate.identifiability.value for e in series).items())
            ),
            "conservation_fail_closed": sum(
                1 for d in series if "CONSERVATION_VIOLATION" in d.estimate.reason_codes
            ),
            "first_conservation_detection_s": next(
                (d.t_s for d in series if "CONSERVATION_VIOLATION" in d.estimate.reason_codes),
                None,
            ),
            "compute_us_mean": 1e6 * sum(compute) / len(compute) if compute else None,
            "compute_us_p99": 1e6 * nearest_rank(compute, 0.99) if compute else None,
        }
    return out


def _count(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for v in values:
        counts[v] += 1
    return counts


def snapshot_rows(workload: Workload, design: Design, decisions, every_s: float = 300.0):
    """Sampled per-decision rows for the report (truth columns come from the scorer side)."""

    rows = []
    for d in decisions:
        if abs(d.t_s % every_s) > 1e-9 and abs(d.t_s % every_s - every_s) > 1e-9:
            continue
        e = d.estimate
        c = true_capacity(workload, d.route_id, d.t_s - WINDOW_S, d.t_s)
        point = e.estimated_capacity_rps
        rows.append({
            "t_s": d.t_s,
            "route_id": d.route_id,
            "true_capacity_rps": c,
            "observed_throughput_rps": e.observed_throughput_rps,
            "estimated_capacity_rps": point,
            "lower_bound_rps": e.capacity_lower_bound_rps,
            "safe_headroom_rps": e.safe_headroom_rps,
            "mode": e.mode.value,
            "route_state": e.route_state.value,
            "identifiability": e.identifiability.value,
            "overestimate_rps": None if point is None else max(0.0, point - c),
            "bound_overestimate_rps": None if e.capacity_lower_bound_rps is None
            else max(0.0, e.capacity_lower_bound_rps - c),
        })  # fmt: skip
    return rows


# ----------------------------------------------------------------- run records


SOURCE_FILES = (
    "intelligence/ml/capacity_estimator.py",
    "intelligence/experiments/capacity_evaluation.py",
    "intelligence/simulation/des_validate.py",
    "intelligence/simulation/engine.py",
    "intelligence/simulation/model_selection.py",
    "intelligence/twin/route_twin.py",
    "intelligence/data/generate_dataset.py",
    "intelligence/simulation/scenarios.py",
)


def source_digest(root: Path | None = None) -> str:
    root = root or Path(__file__).resolve().parents[2]
    h = hashlib.sha256()
    for rel in SOURCE_FILES:
        h.update(rel.encode())
        h.update((root / rel).read_bytes())
    return h.hexdigest()


@dataclass(frozen=True)
class EvaluationRecord:
    design: str
    description: str
    label: str
    seed: int
    scenario_id: str
    workload_content_hash: str
    engine: str
    dt_s: float
    model_status: str
    model_reason: str
    estimator_version: str
    estimator_config_digest: str
    scores: dict
    isolation_errors: int
    rows: tuple
    result_hash: str

    def to_json(self) -> str:
        return canonical_json(asdict(self))


def evaluate_design(
    design: Design,
    estimator_config: CapacityEstimatorConfig | None = None,
    table=VALIDATION_TABLE,
) -> EvaluationRecord:
    cfg = estimator_config or CapacityEstimatorConfig()
    selection = choose_engine(design.config, table)
    workload = generate_workload(design.config)
    observed = run_engine(workload, selection, design.routes)
    decisions, snapshots = feed_estimators(
        design.config, observed.telemetry, design.routes, design.impairments, cfg,
        keep_snapshots=len(design.routes) > 1,
    )  # fmt: skip
    scores = score_run(workload, design, decisions, observed.truth_offered,
                       observed.truth_depth, observed.dt_s)  # fmt: skip
    iso = isolation_errors(snapshots, decisions, cfg) if snapshots else 0
    rows = tuple(snapshot_rows(workload, design, decisions))
    deterministic = {
        "design": design.name,
        "seed": design.config.seed,
        "engine": observed.engine,
        "estimates": [d.estimate.digest() for d in decisions],
        "scores": _strip_timing(scores),
        "rows": rows,
    }
    return EvaluationRecord(
        design=design.name,
        description=design.description,
        label=design.label,
        seed=design.config.seed,
        scenario_id=design.config.scenario_id,
        workload_content_hash=workload.manifest.content_hash,
        engine=observed.engine,
        dt_s=observed.dt_s,
        model_status=selection.status.value,
        model_reason=selection.reason,
        estimator_version=ESTIMATOR_VERSION,
        estimator_config_digest=cfg.digest(),
        scores=scores,
        isolation_errors=iso,
        rows=rows,
        result_hash=sha256_json(deterministic),
    )


def _strip_timing(scores: dict) -> dict:
    return {
        route: {k: v for k, v in s.items() if not k.startswith("compute_")}
        for route, s in scores.items()
    }


# ----------------------------------------------------------------- Track B comparison

TRACKB_ESTIMATOR = {
    "source": "origin/feature/backend 9e736d9: backend/aorts/recovery/capacity.py",
    "algorithm": "point = workers / (switch-measured p50 latency); confidence = sample factor x "
    "reliability factor x stability; low = point x (1 - 0.8 x (1 - confidence))",
    "required_inputs": ("workers", "latency_p50_ms (switch-side)", "samples"),
    "status": "NOT_COMPARABLE",
    "reason": "Track B's point estimate needs the switch's worker-pool size and switch-side "
    "service latency. v1.1 RouteTelemetry (remitter own-traffic scope) carries neither; "
    "supplying the simulator's configured capacity in their place would hand the estimator "
    "hidden truth. No matched comparison is possible without a v1.1 telemetry field for "
    "switch-measured concurrency, so no outperformance claim is made.",
}


def trackb_inputs_missing() -> tuple[str, ...]:
    """Track B inputs absent from the v1.1 telemetry model (empty would allow a comparison)."""

    fields = set(RouteTelemetry.model_fields)
    return tuple(name for name in ("workers", "samples") if name not in fields)


# ----------------------------------------------------------------- recovery-valuable design

RECOVERY_VALUABLE_SPEC = {
    "id": "RV-1",
    "label": "research preparation (Stage 07/08); not used for any AORTS improvement claim",
    "purpose": "evaluate the trade-off between useful recovery (authoritative check answers "
    "for committed payments) and congestion harm (checks competing with payments)",
    "mechanism": {
        "commit_then_lose": "RESPONSE_LOSS fault: the beneficiary decides (credits) the payment "
        "but the confirmation never reaches the remitter, so the payment is ambiguous although "
        "committed",
        "useful_check": "a permitted status check after the first-check delay is served by the "
        "same bottleneck and returns AUTHORITATIVE_SUCCESS/FAILURE for decided payments",
        "contention": "a moderate CAPACITY_CUT on the same window raises utilisation to ~0.875, "
        "so every released check takes service from new payments",
    },
    "unchanged": "S1/S2/S3 and their workload definitions are not modified",
    "truth_boundary": "built only from existing ScenarioConfig fields; the generator draws the "
    "same five random values per payment; nothing hidden reaches observations",
}


def recovery_valuable_design(
    seed: int,
    scale: float = 0.01,
    duration: float = 1500.0,
    loss: float = 0.3,
    cut: float = 0.2,
) -> ScenarioConfig:
    """RV-1: committed payments whose confirmation is lost, under contention."""

    route = (("A1-B", "B", 0.7, 1.0),)
    faults = (
        _cut("A1-B", 300.0, 1200.0, loss, kind="RESPONSE_LOSS"),
        _cut("A1-B", 300.0, 1200.0, cut),
    )
    base = _custom("rv1", seed, route, faults, scale=scale, duration=duration)
    return base.model_copy(
        update={
            "scenario_id": "rv1-commit-then-lose",
            "description": "RV-1 recovery-valuable research design",
        }
    )


# ----------------------------------------------------------------- evidence generator


def main() -> None:  # pragma: no cover - evidence generator
    out = Path("results/processed/stage06")
    out.mkdir(parents=True, exist_ok=True)
    cfg = CapacityEstimatorConfig()
    began = time.perf_counter()
    records = []
    with (out / "capacity_runs.jsonl").open("w", encoding="utf-8") as sink:
        for seed in SMOKE_SEEDS:
            for design in designs(seed):
                record = evaluate_design(design, cfg)
                sink.write(record.to_json() + "\n")
                records.append(record)
                print(design.name, seed, record.engine, record.result_hash[:12], flush=True)
    manifest = {
        "label": "Stage 06 development smoke results (not held-out evidence)",
        "seeds": SMOKE_SEEDS,
        "rate_scale": SMOKE_RATE_SCALE,
        "duration_s": SMOKE_DURATION_S,
        "window_s": WINDOW_S,
        "estimator_version": ESTIMATOR_VERSION,
        "estimator_config": json.loads(cfg.model_dump_json()),
        "estimator_config_digest": cfg.digest(),
        "source_digest": source_digest(),
        "trackb_comparison": TRACKB_ESTIMATOR,
        "trackb_inputs_missing": trackb_inputs_missing(),
        "recovery_valuable_spec": RECOVERY_VALUABLE_SPEC,
        "elapsed_s": round(time.perf_counter() - began, 1),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    (out / "capacity_summary.md").write_text(render_summary(records, manifest))
    files = sorted(p for p in out.iterdir() if p.name != "SHA256SUMS")
    sums = "".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in files)
    (out / "SHA256SUMS").write_text(sums)
    print(sums)


def render_summary(records: Sequence[EvaluationRecord], manifest: dict) -> str:
    def f(x, digits=2):
        if x is None:
            return "-"
        if isinstance(x, float):
            return f"{x:.{digits}f}"
        return str(x)

    lines = [
        "# Stage 06 capacity estimator: development smoke results",
        "",
        "Not held-out evidence. Engine chosen per design by the Stage 04S selector.",
        f"Estimator {manifest['estimator_version']}, "
        f"config {manifest['estimator_config_digest'][:12]}, "
        f"source digest {manifest['source_digest'][:12]}.",
        "",
        "## Per design, seed and route",
        "",
        "| design | seed | route | engine (status) | detect s | bound ok s | recover ack s "
        "| cap seen s | MAE all | unsafe pt | LB cov all | LB viol | H over | insuff | "
        "false id | cons FC | iso err | us/est |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in records:
        for route, s in r.scores.items():
            a = s["periods"]["all"]
            lines.append(
                f"| {r.design} | {r.seed} | {route} | {r.engine} ({r.model_status}) "
                f"| {f(s['congestion_detection_delay_s'], 0)} "
                f"| {f(s['bound_restoration_delay_s'], 0)} | {f(s['recovery_ack_delay_s'], 0)} "
                f"| {f(s['recovered_capacity_detection_delay_s'], 0)} | {f(a['mae_rps'])} "
                f"| {f(a['unsafe_point_rate'], 3)} | {f(a['bound_coverage'], 3)} "
                f"| {a['bound_violations']} | {a['headroom_overestimates']} "
                f"| {f(a['insufficient_fraction'], 3)} | {a['false_identified']} "
                f"| {s['conservation_fail_closed']} | {r.isolation_errors} "
                f"| {f(s['compute_us_mean'], 0)} |"
            )
    lines += ["", "## Periods (A before, B onset 60 s, C sustained, D 300 s after, "
              "E poisoned, F insufficient observation), pooled over seeds", ""]  # fmt: skip
    lines.append("| design | route | period | n | point n | MAE | RMSE | rel err | unsafe pt "
                 "| LB cov | LB viol | H>0 | H over | insuff | false id |")  # fmt: skip
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for (design, route, period), p in sorted(_pool(records).items()):
        lines.append(
            f"| {design} | {route} | {period} | {p['decisions']} | {p['point_decisions']} "
            f"| {f(p['mae_rps'])} | {f(p['rmse_rps'])} | {f(p['mean_rel_error'], 3)} "
            f"| {f(p['unsafe_point_rate'], 3)} | {f(p['bound_coverage'], 3)} "
            f"| {p['bound_violations']} | {p['headroom_positive']} "
            f"| {p['headroom_overestimates']} | {f(p['insufficient_fraction'], 3)} "
            f"| {p['false_identified']} |"
        )
    lines += [
        "",
        "## Sampled decisions (every 300 s, first seed)",
        "",
        "| design | t s | route | true C | seen x | C hat | L | H | mode | state | ident "
        "| C over | L over |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    first = manifest["seeds"][0]
    for r in records:
        if r.seed != first:
            continue
        for row in r.rows:
            lines.append(
                f"| {r.design} | {f(row['t_s'], 0)} | {row['route_id']} "
                f"| {f(row['true_capacity_rps'])} | {f(row['observed_throughput_rps'])} "
                f"| {f(row['estimated_capacity_rps'])} | {f(row['lower_bound_rps'])} "
                f"| {f(row['safe_headroom_rps'])} | {row['mode']} | {row['route_state']} "
                f"| {row['identifiability']} | {f(row['overestimate_rps'])} "
                f"| {f(row['bound_overestimate_rps'])} |"
            )
    lines += ["", f"elapsed {manifest['elapsed_s']} s", ""]
    return "\n".join(lines)


def _pool(records: Sequence[EvaluationRecord]) -> dict:
    """Pool period summaries across seeds from their counts (means weighted by counts).

    Medians cannot be pooled from per-seed summaries, so pooled rows omit them.
    """

    acc: dict = {}
    for r in records:
        for route, s in r.scores.items():
            for period, summ in s["periods"].items():
                a = acc.setdefault((r.design, route, period), defaultdict(float))
                n, b = summ["point_decisions"], summ["bound_decisions"]
                for name in ("decisions", "point_decisions", "bound_decisions",
                             "bound_violations", "headroom_positive", "headroom_overestimates",
                             "false_identified"):  # fmt: skip
                    a[name] += summ[name]
                if n:
                    a["abs"] += summ["mae_rps"] * n
                    a["sq"] += summ["rmse_rps"] ** 2 * n
                    a["unsafe"] += summ["unsafe_point_rate"] * n
                    if summ["mean_rel_error"] is not None:
                        a["rel"] += summ["mean_rel_error"] * n
                        a["rel_n"] += n
                a["insufficient"] += (summ["insufficient_fraction"] or 0.0) * summ["decisions"]
                del b
    pooled = {}
    for key, a in acc.items():
        n, d = a["point_decisions"], a["decisions"]
        pooled[key] = {
            "decisions": int(d),
            "point_decisions": int(n),
            "mae_rps": a["abs"] / n if n else None,
            "rmse_rps": math.sqrt(a["sq"] / n) if n else None,
            "mean_rel_error": a["rel"] / a["rel_n"] if a["rel_n"] else None,
            "unsafe_point_rate": a["unsafe"] / n if n else None,
            "bound_coverage": (
                (a["bound_decisions"] - a["bound_violations"]) / a["bound_decisions"]
                if a["bound_decisions"]
                else None
            ),
            "bound_violations": int(a["bound_violations"]),
            "headroom_positive": int(a["headroom_positive"]),
            "headroom_overestimates": int(a["headroom_overestimates"]),
            "insufficient_fraction": a["insufficient"] / d if d else None,
            "false_identified": int(a["false_identified"]),
        }
    return pooled


if __name__ == "__main__":  # pragma: no cover
    main()
