"""Fair, causal policy-comparison harness (Stage 05).

For one Stage 3A scenario and one baseline policy, the harness:

1. generates the immutable Stage 3A workload (identical for every policy and run order);
2. asks the Stage 04S research-model selector which simulator may carry the evidence, from the
   declared design only (scenario, the policy's declared release bound, timing compliance);
3. runs that simulator in closed loop: at every decision instant the policy receives a
   ``PolicyInput`` built only from what the remitter has observed so far; its requests pass the
   shared safety gate; the simulator applies them (and enforces its own eligibility rules);
   only revealed events are fed back;
4. derives comparable metrics and a serializable ``RunRecord``.

The simulator's ground truth is read only after the run, to compute evaluation metrics. A
policy never receives capacity, faults, beneficiary truth, customer intent or future outcomes.
Stress-test policies (``STRESS_UNCONSTRAINED``) run with the timing policy and per-bank limits
disabled in both the gate and the simulator, and are labelled noncompliant; nothing here, in
any mode, can perform a financial operation.
"""

import math
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field

from contracts.v1_1 import DeadlineRuleKind, FirstCheckReference, TimingPolicy
from intelligence.data.generate_dataset import Workload, generate_workload
from intelligence.experiments.baselines import (
    POLICY_API_VERSION,
    CheckOutcome,
    Policy,
    PolicyAction,
    PolicyCategory,
    PolicyInput,
    TxnView,
    make_policy,
)
from intelligence.experiments.metrics import RawRun, RouteCounts, RunMetrics, derive, sha256_json
from intelligence.simulation import model_validation as mv
from intelligence.simulation.des_validate import (
    CheckRelease,
    CheckRequest,
    DesConfig,
    DesObservation,
    ReleaseFeedback,
    simulate_des,
)
from intelligence.simulation.engine import CheckDecision, FluidSimulation, SimulationConfig
from intelligence.simulation.model_selection import BURST_RELEASE_SIZE, Model, Purpose, select_model
from intelligence.simulation.scenarios import ScenarioConfig

HARNESS_VERSION = "aorts-harness/1"


@dataclass(frozen=True)
class SafetyConfig:
    """Shared safety limits applied identically to every comparable (STRICT) arm."""

    decision_interval_s: float = 1.0
    max_checks_per_bank_per_decision: int = 10  # Track B default bank limit: 10 checks/s


# ------------------------------------------------------------------- remitter tracker


@dataclass
class _Txn:
    transaction_id: str
    route_id: str
    bank_id: str
    sent_s: float
    ambiguous_s: float
    checks_released: int = 0
    last_check_s: float | None = None
    in_flight: str | None = None
    last_result: str | None = None
    resolved_s: float | None = None


class Tracker:
    """The remitter's own view, built only from revealed observations and its own actions."""

    def __init__(self, workload: Workload) -> None:
        scenario = workload.manifest.config
        self.timing = scenario.timing_policy
        self.bank = {r.route_id: r.remitter_bank_id for r in scenario.routes}
        self.sent = {
            p.transaction_id: (p.offset_s, p.route_id) for p in workload.exogenous.payments
        }
        self.txns: dict[str, _Txn] = {}
        self.checks: dict[str, tuple[str, float]] = {}  # check_id -> (txn, released at)
        self.feedback: list[CheckOutcome] = []

    def route_of(self, txn: str) -> str:
        return self.sent[txn.removeprefix("RP-")][1]

    def ambiguous(self, txn: str, observed_at: float) -> None:
        if txn in self.txns:
            return
        route = self.route_of(txn)
        timeout = self.timing.client_timeout_s
        if txn in self.sent:  # an original: the remitter knows when it sent it
            sent = self.sent[txn][0]
            at = sent + timeout
        else:  # a customer re-payment: known only through its observed timeout
            at = observed_at
            sent = at - timeout
        self.txns[txn] = _Txn(txn, route, self.bank[route], sent, at)

    def resolved(self, txn: str, at: float) -> None:
        if txn in self.txns and self.txns[txn].resolved_s is None:
            self.txns[txn].resolved_s = at

    def released(self, txn: str, check_id: str, at: float) -> None:
        t = self.txns[txn]
        t.checks_released += 1
        t.last_check_s = at
        t.in_flight = check_id
        self.checks[check_id] = (txn, at)
        self.feedback.append(CheckOutcome(txn, t.route_id, "RELEASED", at, check_id))

    def refused(self, txn: str, reason: str, at: float) -> None:
        route = self.txns[txn].route_id if txn in self.txns else self.route_of(txn)
        self.feedback.append(CheckOutcome(txn, route, "REFUSED", at, reason))

    def answered(self, check_id: str, result: str, at: float) -> None:
        if check_id not in self.checks:
            return
        txn, released_at = self.checks[check_id]
        t = self.txns[txn]
        if t.in_flight == check_id:
            t.in_flight = None
        t.last_result = result
        self.feedback.append(
            CheckOutcome(txn, t.route_id, "ANSWERED", at, result, at - released_at)
        )

    def timed_out(self, check_id: str, at: float) -> None:
        if check_id not in self.checks:
            return
        txn, released_at = self.checks[check_id]
        t = self.txns[txn]
        if t.in_flight == check_id:
            t.in_flight = None
        self.feedback.append(CheckOutcome(txn, t.route_id, "TIMED_OUT", at, None, at - released_at))

    def outstanding(self) -> list[_Txn]:
        return [t for t in self.txns.values() if t.resolved_s is None]

    def drain_feedback(self) -> tuple[CheckOutcome, ...]:
        out, self.feedback = tuple(self.feedback), []
        return out


def timing_permits(t: _Txn, now: float, timing: TimingPolicy) -> bool:
    """Research TimingPolicy (not a verified regulatory rule) plus the transaction deadline."""

    if t.checks_released >= timing.max_counted_checks:
        return False
    ref = (
        t.sent_s
        if timing.first_check_reference is FirstCheckReference.INITIATED
        else (t.ambiguous_s)
    )
    if now < ref + timing.first_check_delay_s or now > ref + timing.check_window_s:
        return False
    if t.last_check_s is not None and now < t.last_check_s + timing.min_check_spacing_s:
        return False
    rule = timing.deadline_rule
    if rule.kind is DeadlineRuleKind.FIXED_DURATION and now >= t.sent_s + rule.duration_s:
        return False
    return True


def _view(t: _Txn) -> TxnView:
    return TxnView(t.transaction_id, t.route_id, t.bank_id, t.sent_s, t.ambiguous_s,
                   t.checks_released, t.last_check_s, t.in_flight is not None,
                   t.last_result)  # fmt: skip


@dataclass
class GateLog:
    requests: int = 0
    refused: Counter = field(default_factory=Counter)
    not_selected: int = 0


def gate(
    tracker: Tracker,
    action: PolicyAction,
    now: float,
    strict: bool,
    safety: SafetyConfig,
    log: GateLog,
) -> dict[str, list[str]]:
    """Shared safety gate. Returns allowed transactions per route; refusals become feedback."""

    allowed: dict[str, list[str]] = defaultdict(list)
    per_bank: Counter = Counter()
    seen: set[str] = set()
    for txn in action.transaction_ids:
        t = tracker.txns.get(txn)
        known = t is not None or txn.removeprefix("RP-") in tracker.sent
        route = t.route_id if t is not None else (tracker.route_of(txn) if known else None)
        budget = action.budget(route) if route is not None else None
        if budget is not None and len(allowed[route]) >= budget:
            log.not_selected += 1  # beyond the policy's own budget: never requested
            continue
        log.requests += 1
        reason = None
        if txn in seen:
            reason = "duplicate_request"
        elif t is None or t.resolved_s is not None:
            reason = "not_outstanding"
        elif t.in_flight is not None:
            reason = "in_flight"
        elif strict and not timing_permits(t, now, tracker.timing):
            reason = "timing_policy"
        seen.add(txn)
        if reason is not None:
            log.refused[reason] += 1
            if known:
                tracker.refused(txn, reason, now)
            continue
        if strict and per_bank[t.bank_id] >= safety.max_checks_per_bank_per_decision:
            log.refused["rate_limited"] += 1
            tracker.refused(txn, "rate_limited", now)
            continue
        per_bank[t.bank_id] += 1
        allowed[t.route_id].append(txn)
    return allowed


def policy_input(tracker: Tracker, routes, now: float, k: int, strict: bool) -> PolicyInput:
    outstanding = sorted(tracker.outstanding(), key=lambda t: (t.ambiguous_s, t.transaction_id))
    eligible = [
        t
        for t in outstanding
        if t.in_flight is None and (not strict or timing_permits(t, now, tracker.timing))
    ]
    return PolicyInput(
        now_s=now,
        decision_index=k,
        routes=tuple(routes),
        outstanding=tuple(_view(t) for t in outstanding),
        eligible=tuple(_view(t) for t in eligible),
        feedback=tracker.drain_feedback(),
        timing=tracker.timing,
    )


@dataclass
class _ReleaseAudit:
    invalid: int = 0
    noncompliant: int = 0

    def check(self, tracker: Tracker, txn: str, now: float, bank_counts: Counter, safety) -> None:
        t = tracker.txns.get(txn)
        if t is None or t.resolved_s is not None or t.in_flight is not None:
            self.invalid += 1
            return
        bank_counts[t.bank_id] += 1
        if (
            not timing_permits(t, now, tracker.timing)
            or bank_counts[t.bank_id] > safety.max_checks_per_bank_per_decision
        ):
            self.noncompliant += 1


# ----------------------------------------------------------------------- engines


def _is_decision(t: float, interval: float) -> bool:
    k = round(t / interval)
    return math.isclose(k * interval, t, abs_tol=1e-9)


def _run_fluid(workload, policy, strict, safety, dt):
    scenario = workload.manifest.config
    routes = [r.route_id for r in scenario.routes]
    epoch = scenario.epoch
    tracker, log, audit = Tracker(workload), GateLog(), _ReleaseAudit()
    sim = FluidSimulation(workload, SimulationConfig(dt_s=dt, enforce_timing_policy=strict))
    payment_lat, resolution_lat, outcome_times = [], [], {}
    k = 0
    while not sim.done:
        now = sim.tick * dt
        decisions, requested = [], {}
        if _is_decision(now, safety.decision_interval_s):
            action = policy.decide(policy_input(tracker, routes, now, k, strict))
            k += 1
            allowed = gate(tracker, action, now, strict, safety, log)
            for route, txns in sorted(allowed.items()):
                if txns:
                    decisions.append(CheckDecision(route_id=route, transaction_ids=tuple(txns)))
                    requested[route] = txns
        obs = sim.step(decisions)
        denied = set(obs.denied_checks)
        banks: Counter = Counter()
        for txns in requested.values():
            for txn in txns:
                if txn in denied:
                    tracker.refused(txn, "engine_ineligible", now)
                    continue
                audit.check(tracker, txn, now, banks, safety)
                n = tracker.txns[txn].checks_released + 1
                tracker.released(txn, f"CK-{txn}-{n}", now)
        engine_refused = len(denied)
        log.refused["__engine__"] += engine_refused
        end = (obs.window_end - epoch).total_seconds()
        for txn in obs.newly_ambiguous:
            tracker.ambiguous(txn, end)
        for o in obs.outcomes:
            at = (o.observed_at - epoch).total_seconds()
            if o.how == "TIMELY_RESPONSE":
                if o.transaction_id in tracker.sent:
                    payment_lat.append(at - tracker.sent[o.transaction_id][0])
            else:
                tracker.resolved(o.transaction_id, at)
                t = tracker.txns.get(o.transaction_id)
                if t is not None:
                    resolution_lat.append(at - t.ambiguous_s)
                    outcome_times[o.transaction_id] = at
        for c in obs.check_results:
            tracker.answered(c.check_id, c.result.value, (c.observed_at - epoch).total_seconds())
        for check_id in obs.check_timeouts:
            tracker.timed_out(check_id, end)
    result = sim.result()
    counts = {r: RouteCounts() for r in routes}
    for m in result.metrics:
        c = counts[m.route_id]
        c.new_ambiguous += m.new_ambiguous
        c.resolved += m.resolved
        c.checks_released += m.checks_released
        c.checks_answered += m.check_results
        c.checks_timed_out += m.wasted_checks
    last = {m.route_id: m for m in result.metrics}
    for r in routes:
        counts[r].unresolved_at_end = last[r].outstanding_ambiguous
    delays = {r: [m.est_queue_delay_s for m in result.metrics if m.route_id == r] for r in routes}
    return (
        tracker, log, audit, counts, result.total("resolved_by_check"), payment_lat,
        resolution_lat, outcome_times, delays, dt, result.digest,
    )  # fmt: skip


def _run_des(workload, policy, strict, safety):
    scenario = workload.manifest.config
    routes = [r.route_id for r in scenario.routes]
    tracker, log, audit = Tracker(workload), GateLog(), _ReleaseAudit()
    state = {"k": 0, "engine_refused": 0}
    observe = getattr(policy, "observe", None)

    def controller(now: float, obs: DesObservation, fb: ReleaseFeedback | None):
        if fb is not None:
            banks: Counter = Counter()
            for txn, check_id in fb.released:
                audit.check(tracker, txn, fb.decided_at_s, banks, safety)
                tracker.released(txn, check_id, fb.decided_at_s)
            for txn, reason in fb.refused:
                state["engine_refused"] += 1
                tracker.refused(txn, f"engine_{reason}", fb.decided_at_s)
        for txn in obs.newly_ambiguous:
            tracker.ambiguous(txn, obs.end_s)
        for txn, _, _, at in obs.resolutions:
            tracker.resolved(txn, at)
        for check_id, _, result, at in obs.check_results:
            tracker.answered(check_id, result, at)
        for check_id in obs.check_timeouts:
            tracker.timed_out(check_id, obs.end_s)
        if observe is not None:  # policies that consume remitter-own traffic (Stage 12 AORTS)
            observe(obs)
        action = policy.decide(policy_input(tracker, routes, now, state["k"], strict))
        state["k"] += 1
        allowed = gate(tracker, action, now, strict, safety, log)
        return [CheckRequest(r, tuple(t)) for r, t in sorted(allowed.items()) if t]

    des = simulate_des(
        workload,
        DesConfig(bin_s=1.0, enforce_timing_policy=strict),
        (),
        controller,
        safety.decision_interval_s,
    )
    log.refused["__engine__"] += state["engine_refused"]
    counts = {r: RouteCounts() for r in routes}
    for i in des.intervals:
        c = counts[i.route_id]
        c.new_ambiguous += i.new_ambiguous
        c.resolved += i.resolved
        c.checks_released += i.checks_released
        c.checks_answered += i.check_results
        c.checks_timed_out += i.wasted_checks
    for route, ids in des.observations[-1].ambiguous_outstanding:
        counts[route].unresolved_at_end = len(ids)
    timeout = scenario.timing_policy.client_timeout_s
    payment_lat, resolution_lat, outcome_times = [], [], {}
    for t in des.traces:
        if t.kind == "payment" and t.response_s is not None and t.response_s <= t.sent_s + timeout:
            payment_lat.append(t.response_s - t.sent_s)
        if t.ambiguous_s is not None and t.resolved_s is not None:
            resolution_lat.append(t.resolved_s - t.ambiguous_s)
            outcome_times[t.txn] = t.resolved_s
    delays = {}
    for r in routes:
        cap = mv.capacity_series(workload, r, des.bins)
        rows = [i for i in des.intervals if i.route_id == r]
        delays[r] = [
            i.bottleneck_queue_depth / c if c > 0 else None for i, c in zip(rows, cap, strict=True)
        ]
    return (
        tracker, log, audit, counts, des.total("resolved_by_check"), payment_lat, resolution_lat,
        outcome_times, delays, 1.0, des.digest,
    )  # fmt: skip


# ------------------------------------------------------------------------- runs


@dataclass(frozen=True)
class RunRecord:
    harness_version: str
    policy_api_version: str
    policy_id: str
    policy_version: str
    category: str
    mirrors: str | None
    scenario_id: str
    seed: int
    workload_content_hash: str
    purpose: str
    engine: str
    model_status: str  # this arm's own selector verdict
    model_reason: str
    engine_decision: str  # why this engine/timestep (comparison-group rule or forced)
    approximate: bool
    dt_s: float
    timing_policy_id: str
    timing_compliant: bool
    released: int
    harness_released: int  # releases the remitter recorded; must equal ``released`` (truth)
    rejected: int
    metrics: RunMetrics
    engine_digest: str
    policy_state_hash: str
    result_hash: str

    def to_json(self) -> str:
        from intelligence.experiments.metrics import canonical_json

        return canonical_json(asdict(self))


def declared_schedule(policy: Policy, routes: Sequence[str], safety: SafetyConfig, strict: bool):
    """The design-level release bound used for model selection (no outcome is consulted)."""

    bound = policy.info.max_release_per_decision
    if strict:
        cap = safety.max_checks_per_bank_per_decision
        bound = cap if bound is None else min(bound, cap)
    elif bound is None:
        bound = BURST_RELEASE_SIZE * 10
    if not bound:
        return ()
    return tuple(CheckRelease(0.0, r, bound) for r in routes)


def arm_selection(scenario, policy, purpose, safety, dt_s=None):
    strict = policy.info.category is not PolicyCategory.STRESS_UNCONSTRAINED
    routes = [r.route_id for r in scenario.routes]
    return select_model(
        scenario,
        declared_schedule(policy, routes, safety, strict),
        purpose,
        dt_s=dt_s,
        timing_compliant=strict,
    )


def group_engine(
    scenario: ScenarioConfig,
    policy_ids: Sequence[str],
    purpose: Purpose,
    safety: SafetyConfig,
) -> tuple[Model, float, str]:
    """One engine and timestep for every arm compared together (never mixed per policy).

    Fluid only if every arm is fluid-permitted at a common timestep; otherwise the DES.
    """

    policies = [make_policy(p) for p in policy_ids]
    selections = [arm_selection(scenario, p, purpose, safety) for p in policies]
    if all(sel.model is Model.FLUID for sel in selections):
        dt = min(sel.dt_s for sel in selections)
        again = [arm_selection(scenario, p, purpose, safety, dt) for p in policies]
        if all(sel.model is Model.FLUID for sel in again):
            return Model.FLUID, dt, f"group: every arm fluid-permitted at dt={dt}"
    blocking = sorted(
        p.info.policy_id
        for p, sel in zip(policies, selections, strict=True)
        if sel.model is not Model.FLUID
    )
    return Model.DES, 1.0, f"group: DES because {', '.join(blocking)} not fluid-permitted"


def run_comparison(
    scenario: ScenarioConfig,
    policy_ids: Sequence[str],
    purpose: Purpose = Purpose.PRIMARY,
    safety: SafetyConfig | None = None,
) -> list[RunRecord]:
    """Comparable arms (REFERENCE/STRICT) share one engine; stress arms form their own group."""

    safety = safety or SafetyConfig()
    groups: dict[bool, list[str]] = {True: [], False: []}
    for pid in policy_ids:
        stress = make_policy(pid).info.category is PolicyCategory.STRESS_UNCONSTRAINED
        groups[stress].append(pid)
    records = []
    for members in groups.values():
        if not members:
            continue
        engine, dt, why = group_engine(scenario, members, purpose, safety)
        records.extend(
            run_policy(scenario, pid, purpose, safety, engine, dt, why) for pid in members
        )
    order = {pid: i for i, pid in enumerate(policy_ids)}
    return sorted(records, key=lambda r: order[r.policy_id])


def run_policy(
    scenario: ScenarioConfig,
    policy_id: str,
    purpose: Purpose = Purpose.PRIMARY,
    safety: SafetyConfig | None = None,
    engine: Model | None = None,
    dt_s: float | None = None,
    engine_decision: str | None = None,
    policy: Policy | None = None,
) -> RunRecord:
    """One closed-loop run. Without ``engine`` the arm's own selection decides (single runs);
    comparisons pass the group engine from ``run_comparison``. ``policy`` supplies an already
    built policy (Stage 12 AORTS arms need the scenario); ``policy_id`` must match it."""

    safety = safety or SafetyConfig()
    workload = generate_workload(scenario)
    policy = policy or make_policy(policy_id)
    if policy.info.policy_id != policy_id:
        raise ValueError(f"policy {policy.info.policy_id} does not match {policy_id}")
    if policy.info.category in (PolicyCategory.NOT_COMPARABLE, PolicyCategory.TIER2_EXPERIMENTAL):
        raise ValueError(f"{policy_id} cannot be run as a Stage 05 baseline")
    strict = policy.info.category is not PolicyCategory.STRESS_UNCONSTRAINED
    routes = [r.route_id for r in scenario.routes]
    selection = arm_selection(scenario, policy, purpose, safety)
    if engine is None:
        chosen = selection.model
        dt = selection.dt_s if chosen is Model.FLUID else 1.0
        engine_decision = engine_decision or "single run: arm selection"
    else:
        chosen, dt = engine, (dt_s if dt_s is not None else 1.0)
        engine_decision = engine_decision or f"forced: {engine.value}"
    if chosen is Model.FLUID and hasattr(policy, "observe"):
        raise ValueError(f"{policy_id} needs the DES's causal remitter observations")
    if chosen is Model.FLUID:
        out = _run_fluid(workload, policy, strict, safety, dt)
    else:
        out = _run_des(workload, policy, strict, safety)
    tracker, log, audit, counts, by_check, pay_lat, res_lat, outcome_times, delays, dt, digest = out
    timing = scenario.timing_policy
    deadline = timing.deadline_rule.duration_s or math.inf
    horizon = scenario.duration_s
    eligible = censored = before = 0
    for t in tracker.txns.values():
        if t.sent_s + deadline <= horizon:
            eligible += 1
            done = outcome_times.get(t.transaction_id)
            before += done is not None and done <= t.sent_s + deadline
        else:
            censored += 1
    recoveries = []
    for r in routes:
        end = mv.fault_end(workload, r)
        if end is not None:
            recoveries.append(mv.recovery_time(delays[r], end, dt))
    recovery = None
    if recoveries:
        recovery = None if any(x is None for x in recoveries) else max(recoveries)
    engine_refused = log.refused.pop("__engine__", 0)
    raw = RawRun(
        engine=chosen.value,
        horizon_s=horizon,
        routes=counts,
        resolved_by_check=by_check,
        payment_latencies_s=pay_lat,
        resolution_latencies_s=res_lat,
        resolved_before_deadline=before,
        deadline_eligible=eligible,
        deadline_censored=censored,
        time_to_recovery_s=recovery,
        check_requests=log.requests,
        gate_refused={k: v for k, v in log.refused.items() if v},
        engine_refused=engine_refused,
        not_selected=log.not_selected,
        invalid_releases=audit.invalid,
        noncompliant_releases=audit.noncompliant,
    )
    metrics = derive(raw)
    body = {
        "policy": policy.info.policy_id,
        "scenario": scenario.scenario_id,
        "seed": scenario.seed,
        "engine": chosen.value,
        "dt": dt,
        "metrics": asdict(metrics),
        "engine_digest": digest,
    }
    return RunRecord(
        harness_version=HARNESS_VERSION,
        policy_api_version=POLICY_API_VERSION,
        policy_id=policy.info.policy_id,
        policy_version=policy.info.version,
        category=policy.info.category.value,
        mirrors=policy.info.mirrors,
        scenario_id=scenario.scenario_id,
        seed=scenario.seed,
        workload_content_hash=workload.manifest.content_hash,
        purpose=purpose.value,
        engine=chosen.value,
        model_status=selection.status.value,
        model_reason=selection.reason,
        engine_decision=engine_decision,
        approximate=selection.approximate,
        dt_s=dt,
        timing_policy_id=timing.policy_id,
        timing_compliant=strict,
        released=metrics.checks_released,
        harness_released=sum(t.checks_released for t in tracker.txns.values()),
        rejected=metrics.gate_refused + metrics.engine_refused,
        metrics=metrics,
        engine_digest=digest,
        policy_state_hash=sha256_json(dict(policy.state())),
        result_hash=sha256_json(body),
    )


# ------------------------------------------------------------------ smoke experiments

SMOKE_SEED = 3001  # unused elsewhere in the repository
SMOKE_POLICIES = (
    "no-check",
    "static-polling",
    "exp-backoff-jitter",
    "fixed-budget",
    "aimd",
    "naive-polling",
)


def smoke_scenarios() -> dict[str, ScenarioConfig]:
    """Small deterministic designs (1/100 scale). Labels describe the intended condition.

    No separate "no eligible checks" design is needed: in ``normal_load`` and
    ``temporary_fault`` every ambiguous payment resolves before the research policy's 90 s
    first-check point, so no strict policy can release a check (asserted in the tests).
    """

    from intelligence.simulation.scenarios import ScenarioOptions, reference_scenario

    def ref(name, **options):
        return reference_scenario(
            name,
            seed=SMOKE_SEED,
            experiment_id="EXP-S05-SMOKE",
            rate_scale=0.01,
            duration_s=2400.0,
            options=ScenarioOptions(**options),
        )

    repay = mv._scenario(
        "repay-feedback", (mv._route(),), (mv._cut(100.0, 400.0),), repay=1.0, duration=900.0,
        seed=SMOKE_SEED,
    )  # fmt: skip
    return {
        "normal_load": ref("S1"),
        "temporary_fault": ref("S3"),
        "prolonged_degradation": ref("S2"),
        "repayment_feedback": repay,
        "payment_bursts": ref("S1", bursty=True),
        "healthy_parallel_route": ref("S2", healthy_parallel_route=True),
    }


def main() -> None:  # pragma: no cover - evidence generator
    import json
    import time
    from pathlib import Path

    out = Path("results/processed/stage05")
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    began = time.perf_counter()
    with (out / "smoke_runs.jsonl").open("w", encoding="utf-8") as sink:
        for label, scenario in smoke_scenarios().items():
            for record in run_comparison(scenario, SMOKE_POLICIES):
                sink.write(record.to_json() + "\n")
                rows.append((label, record))
    lines = [
        "| scenario | policy | category | engine (dt) | arm status | released | refused "
        "| answered | timed out | by check | new amb | unresolved | p95 pay (s) | recovery (s) "
        "| goodput | noncompliant | result hash |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for label, r in rows:
        m = r.metrics

        def f(x):
            return "-" if x is None else (f"{x:.2f}" if isinstance(x, float) else str(x))

        lines.append(
            f"| {label} | {r.policy_id} | {r.category} | {r.engine} ({r.dt_s}) | {r.model_status} "
            f"| {m.checks_released} | {r.rejected} | {m.checks_answered} | {m.checks_timed_out} "
            f"| {m.resolved_by_check} | {m.new_ambiguous} | {m.unresolved_at_end} "
            f"| {f(m.payment_latency_p95_s)} | {f(m.time_to_recovery_s)} "
            f"| {f(m.recovery_goodput)} | {m.noncompliant_releases} | {r.result_hash[:12]} |"
        )
    summary = "\n".join(lines) + f"\n\nelapsed {time.perf_counter() - began:.0f} s\n"
    (out / "smoke_summary.md").write_text(summary, encoding="utf-8")
    print(summary)
    print(json.dumps({label: r.engine_decision for label, r in rows[:: len(SMOKE_POLICIES)]}))


if __name__ == "__main__":  # pragma: no cover
    main()
