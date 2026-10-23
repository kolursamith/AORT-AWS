"""Stage 12: the AORTS decision pipeline as a closed-loop policy of the Stage 05 harness.

At every decision instant, for every route, the policy runs the same code as the controller
service (Stages 06-08):

1. the remitter's own payment sends and responses of the last window become a
   ``REMITTER_OWN_TRAFFIC`` telemetry sample (``capacity_evaluation.des_telemetry`` semantics,
   computed causally) delivered to the Route Twin as an AUTHENTICATED, unflagged envelope;
2. the Stage 06 ``CapacityEstimator`` updates from the twin snapshot;
3. the transaction pool is built from what the remitter has observed (the Stage 07
   ``lookahead_evaluation.observe`` protocol, causally): ambiguous payments, checks used, last
   check result, the route state when each payment was sent;
4. Stage 08 ``decide`` (look-ahead + objective + ranker) returns a v1.1 ``RecoveryDecision``;
5. at most ``budget_rps x decision interval`` of its ranked selection is requested. The
   harness's shared safety gate and the simulator's own eligibility rules apply as for every
   other arm.

The policy never sees capacity, faults, outcomes or customer behaviour. It needs the DES's
causal remitter observations (``DesObservation.payments_sent`` / ``payment_responses``), so
the Stage 12 runner uses the DES for every arm compared with it.

Research impairments (``Impairment``) change only what the policy observes: throughput
inflation (poisoning), withheld windows (stale/silent telemetry) and controller outages, during
which the arm behaves as Track B's labelled fallback (oldest first, ``fallback_per_route``).
"""

from collections import Counter, deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta

from contracts.v1_1 import SCHEMA_VERSION, RouteTelemetry, ValidatedRouteTelemetry
from intelligence.common.clock import ManualClock
from intelligence.controller.lookahead import CheckCandidate, CheckEvidence, CheckOutcomeCounts
from intelligence.controller.optimizer import (
    RESEARCH_ONLY,
    Ablation,
    DecisionContext,
    OptimizerConfig,
    RouteObservables,
    decide,
)
from intelligence.controller.ranker import PoolEntry
from intelligence.experiments.baselines import (
    PolicyAction,
    PolicyCategory,
    PolicyInfo,
    PolicyInput,
)
from intelligence.experiments.metrics import nearest_rank
from intelligence.ml.capacity_estimator import (
    CapacityEstimator,
    CapacityEstimatorConfig,
    RouteState,
)
from intelligence.simulation.scenarios import ScenarioConfig
from intelligence.twin.route_twin import RouteTwinConfig, RouteTwinRegistry

AORTS_POLICY_VERSION = "aorts-policy-adapter/1"
WINDOW_S = 1.0
OUTCOME_MEMORY = 200  # as ControllerConfig.outcome_memory
HAZARD_WINDOW_S = 60.0  # as lookahead_evaluation.observe
LATENCY_WINDOWS = 10
BANK_LIMIT = 10  # SafetyConfig.max_checks_per_bank_per_decision

ARMS: dict[str, tuple[Ablation, str]] = {
    "aorts": (Ablation.FULL, "Full AORTS: trust-aware capacity, feedback look-ahead, ranking."),
    "aorts-no-feedback": (Ablation.NO_FEEDBACK, "Ablation: forecast without check feedback."),
    "aorts-no-lookahead": (Ablation.NO_LOOKAHEAD, "Ablation: budget = safe cap, no forecast."),
    "aorts-no-uncertainty": (
        Ablation.NO_UNCERTAINTY_BOUND,
        "Research-only ablation: central capacity instead of the conservative bound.",
    ),
    "aorts-no-deadline": (Ablation.NO_DEADLINE_RANKING, "Ablation: age order, no deadlines."),
    "aorts-fixed": (Ablation.FIXED_BUDGET, "Research reference: fixed budget through AORTS."),
}


@dataclass(frozen=True)
class Impairment:
    """What the policy is shown, never what the simulator does (research conditions)."""

    inflate_from_s: float | None = None  # telemetry throughput multiplied from this time
    inflate_until_s: float | None = None
    inflate_factor: float = 1.0
    silent: tuple[float, float] | None = None  # no telemetry delivered in [start, end)
    outage: tuple[float, float] | None = None  # controller unavailable in [start, end)
    fallback_per_route: int = 2  # Track B fallback_budget_per_route during an outage

    def inflated(self, t: float) -> bool:
        return (
            self.inflate_from_s is not None
            and t >= self.inflate_from_s
            and (self.inflate_until_s is None or t < self.inflate_until_s)
        )

    @staticmethod
    def within(window: tuple[float, float] | None, t: float) -> bool:
        return window is not None and window[0] <= t < window[1]


@dataclass
class _Route:
    pending: dict[str, tuple[float, float]] = field(default_factory=dict)  # txn -> sent, due
    sends: int = 0
    responses: int = 0
    timeouts: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    sequence: int = 0
    windows: deque = field(default_factory=lambda: deque(maxlen=LATENCY_WINDOWS))
    states: dict[float, RouteState] = field(default_factory=dict)
    outcomes: deque = field(default_factory=lambda: deque(maxlen=OUTCOME_MEMORY))
    late: deque = field(default_factory=deque)  # times of non-timely resolutions
    backlog: deque = field(default_factory=deque)  # (time, ambiguous outstanding)
    last_window: RouteTelemetry | None = None
    estimate: object | None = None


class AortsPolicy:
    """One AORTS arm (FULL or an ablation) for one scenario run."""

    def __init__(
        self,
        scenario: ScenarioConfig,
        policy_id: str = "aorts",
        *,
        label: str | None = None,
        estimator: CapacityEstimatorConfig | None = None,
        optimizer: OptimizerConfig | None = None,
        impairment: Impairment | None = None,
    ) -> None:
        ablation, description = ARMS[policy_id]
        research = ablation in RESEARCH_ONLY
        self.info = PolicyInfo(
            label or policy_id,
            AORTS_POLICY_VERSION,
            PolicyCategory.STRICT,
            description + (" (research-only)" if research else ""),
            None,
            BANK_LIMIT,
        )
        self.scenario = scenario
        self.exp = scenario.experiment_id
        self.epoch = scenario.epoch
        self.timing = scenario.timing_policy
        self.config = (optimizer or OptimizerConfig()).model_copy(
            update={"ablation": ablation, "allow_research_ablations": research}
        )
        self.impairment = impairment or Impairment()
        self.clock = ManualClock(self.epoch)
        self.twin = RouteTwinRegistry(
            RouteTwinConfig(
                model_version="route-twin-1",
                max_observation_age_s=30.0,
                max_future_skew_s=1.0,
                freshness_threshold_s=5.0,
            ),
            self.clock,
        )
        self.routes: dict[str, _Route] = {}
        self.estimators: dict[str, CapacityEstimator] = {}
        for spec in scenario.routes:
            rid = spec.route_id
            self.twin.register_route(spec.descriptor(self.exp), [self._source(rid)])
            self.estimators[rid] = CapacityEstimator(self.exp, rid, estimator)
            self.routes[rid] = _Route()
        self.ambiguous_at: dict[str, float] = {}
        self.decisions: Counter = Counter()  # (route, mode/reason) -> count, for the record
        self.budget_ticks: Counter = Counter()  # route -> decisions with positive budget
        self.fallback_ticks = 0

    @staticmethod
    def _source(route_id: str) -> str:
        return f"sim-remitter-{route_id}"

    # ----------------------------------------------------------------- observation

    def observe(self, obs) -> None:
        """Remitter-observable facts of one DES bin ``[obs.start_s, obs.end_s)``."""

        timeout = self.timing.client_timeout_s
        for txn, route, _kind, sent in obs.payments_sent:
            r = self.routes.get(route)
            if r is not None:
                r.sends += 1
                r.pending[txn] = (sent, sent + timeout)
        for txn, route, _kind, sent, at in obs.payment_responses:
            r = self.routes.get(route)
            if r is None:
                continue
            r.responses += 1
            r.latencies_ms.append((at - sent) * 1000.0)
            r.pending.pop(txn, None)  # answered (timely); a late one was already counted
        for r in self.routes.values():  # client timeouts that passed in this bin
            for txn, (_sent, due) in list(r.pending.items()):
                if due < obs.end_s:
                    r.timeouts += 1
                    del r.pending[txn]
        for txn in obs.newly_ambiguous:
            self.ambiguous_at[txn] = obs.end_s  # observed at the bin end (conservative)
        for txn, _, how, at in obs.resolutions:
            route = self._route_of(txn)
            if route is not None and how != "TIMELY_RESPONSE":
                self.routes[route].late.append(at)
        for _, txn, result, _at in obs.check_results:
            route = self._route_of(txn)
            if route is not None:
                self.routes[route].outcomes.append(result)
        for check_id in obs.check_timeouts:
            route = self._route_of(check_id.removeprefix("CK-").rsplit("-", 1)[0])
            if route is not None:
                self.routes[route].outcomes.append(None)
        for route, ids in obs.ambiguous_outstanding:
            if route in self.routes:
                self.routes[route].backlog.append((obs.end_s, len(ids)))
        if obs.end_s > obs.start_s:  # the first decision (t = 0) has an empty window
            self._close_windows(obs.start_s, obs.end_s)

    def _route_of(self, txn: str) -> str | None:
        return self._txn_route.get(txn.removeprefix("RP-"))

    @property
    def _txn_route(self) -> Mapping[str, str]:
        if not hasattr(self, "_route_map"):
            from intelligence.data.generate_dataset import generate_workload

            payments = generate_workload(self.scenario).exogenous.payments
            self._route_map = {p.transaction_id: p.route_id for p in payments}
        return self._route_map

    def _close_windows(self, start_s: float, end_s: float) -> None:
        end = self.epoch + timedelta(seconds=end_s)
        start = self.epoch + timedelta(seconds=start_s)
        self.clock.set_time(end)
        imp = self.impairment
        for rid, r in self.routes.items():
            lat = sorted(r.latencies_ms)
            determined = r.responses + r.timeouts
            span = end_s - start_s
            obs = RouteTelemetry(
                schema_version=SCHEMA_VERSION,
                experiment_id=self.exp,
                route_id=rid,
                source_id=self._source(rid),
                observation_scope="REMITTER_OWN_TRAFFIC",
                sequence=r.sequence,
                observation_window_start=start,
                observation_window_end=end,
                observation_timestamp=end,
                payment_rps=r.sends / span,
                check_rps=0.0,
                observed_throughput_rps=r.responses / span,
                completed_payment_rps=r.responses / span,
                completed_check_rps=0.0,
                p50_latency_ms=nearest_rank(lat, 0.5),
                p95_latency_ms=nearest_rank(lat, 0.95),
                latency_sample_count=len(lat),
                timeout_rate=r.timeouts / determined if determined else None,
                timeout_count=r.timeouts,
                timeout_denominator_count=determined,
                queue_depth=len(r.pending),
            )
            r.sequence += 1
            r.sends = r.responses = r.timeouts = 0
            r.latencies_ms = []
            if imp.inflated(end_s):
                f = imp.inflate_factor
                obs = obs.model_copy(
                    update={
                        "observed_throughput_rps": obs.observed_throughput_rps * f,
                        "completed_payment_rps": obs.completed_payment_rps * f,
                    }
                )
            if not imp.within(imp.silent, start_s):
                self.twin.ingest(
                    ValidatedRouteTelemetry(
                        schema_version=SCHEMA_VERSION,
                        experiment_id=self.exp,
                        route_id=rid,
                        telemetry=obs,
                        trust_status="AUTHENTICATED",
                        validator_id="sim-validator",
                        validator_version="sim-1",
                        validated_at=end,
                        cross_check_status="UNAVAILABLE",
                    )
                )
                r.windows.append(obs)
                r.last_window = obs
            r.estimate = self.estimators[rid].update(self.twin.snapshot(self.exp, rid), end)
            r.states[end_s] = r.estimate.route_state

    # ------------------------------------------------------------------- decision

    def decide(self, observed: PolicyInput) -> PolicyAction:
        now = observed.now_s
        if Impairment.within(self.impairment.outage, now):
            return self._fallback(observed)
        quota: Counter = Counter()
        chosen: list[str] = []
        budgets = []
        for rid in observed.routes:
            r = self.routes[rid]
            if r.estimate is None:
                budgets.append((rid, 0))
                continue
            views = [v for v in observed.outstanding if v.route_id == rid]
            bank = views[0].bank_id if views else self.scenario.route(rid).remitter_bank_id
            ctx = DecisionContext(
                experiment_id=self.exp,
                route_id=rid,
                decision_time=self.epoch + timedelta(seconds=now),
                snapshot=self.twin.snapshot(self.exp, rid),
                capacity=r.estimate,
                pool=self._pool(rid, views),
                observables=self._observables(r, len(views), now),
                policy=self.timing,
                bank_quota={bank: max(0, BANK_LIMIT - quota[bank])},
            )
            decision = decide(ctx, self.config).decision
            self.decisions[(rid, decision.decision_mode.value, decision.reason_code.value)] += 1
            per_interval = int(decision.budget_rps * 1.0)  # decision interval 1 s
            take = list(decision.selected_transaction_ids[:per_interval])
            if decision.budget_rps > 0:
                self.budget_ticks[rid] += 1
            quota[bank] += len(take)
            chosen.extend(take)
            budgets.append((rid, len(take)))
        return PolicyAction(tuple(chosen), tuple(budgets))

    def _fallback(self, observed: PolicyInput) -> PolicyAction:
        self.fallback_ticks += 1
        n = self.impairment.fallback_per_route
        chosen = []
        for rid in observed.routes:
            views = sorted(
                (v for v in observed.eligible if v.route_id == rid),
                key=lambda v: (v.ambiguous_s, v.transaction_id),
            )
            chosen.extend(v.transaction_id for v in views[:n])
        return PolicyAction(tuple(chosen), tuple((rid, n) for rid in observed.routes))

    def _pool(self, rid: str, views) -> tuple[PoolEntry, ...]:
        policy = self.timing
        deadline_s = policy.deadline_rule.duration_s or 3600.0
        states = self.routes[rid].states
        entries = []
        for v in views:
            if v.checks_released >= policy.max_counted_checks:
                continue
            eligible = v.sent_s + policy.first_check_delay_s
            evidence = CheckEvidence.NO_CHECK_YET
            if v.last_check_s is not None:
                eligible = max(eligible, v.last_check_s + policy.min_check_spacing_s)
                evidence = {
                    "UNKNOWN": CheckEvidence.LAST_CHECK_UNKNOWN,
                    "PENDING": CheckEvidence.LAST_CHECK_PENDING,
                    None: CheckEvidence.LAST_CHECK_TIMED_OUT,
                }.get(v.last_result, CheckEvidence.NO_CHECK_YET)
            if v.in_flight:
                eligible = max(eligible, (v.last_check_s or 0.0) + policy.min_check_spacing_s)
            congested = states.get(float(int(v.sent_s)) + 1.0) is RouteState.CONGESTED
            entries.append(
                PoolEntry(
                    candidate=CheckCandidate(
                        experiment_id=self.exp,
                        route_id=rid,
                        transaction_id=v.transaction_id,
                        version=1 + v.checks_released,
                        eligible_at=self.epoch + timedelta(seconds=eligible),
                        checks_used=v.checks_released,
                        deadline_at=self.epoch + timedelta(seconds=v.sent_s + deadline_s),
                        evidence=evidence,
                        sent_while_congested=congested,
                        remitter_bank_id=v.bank_id,
                    ),
                    ambiguity_created_at=self.epoch + timedelta(seconds=v.ambiguous_s),
                    deadline_escalated=False,
                )
            )
        return tuple(entries)

    def _observables(self, r: _Route, backlog: int, now: float) -> RouteObservables:
        while r.late and r.late[0] < now - HAZARD_WINDOW_S:
            r.late.popleft()
        while r.backlog and r.backlog[0][0] <= now - HAZARD_WINDOW_S:
            r.backlog.popleft()
        mean_out = sum(n for _, n in r.backlog) / len(r.backlog) if r.backlog else 0.0
        hazard = len(r.late) / (mean_out * HAZARD_WINDOW_S) if mean_out > 0 else None
        recent = [
            w.p50_latency_ms
            for w in r.windows
            if w.p50_latency_ms is not None and w.latency_sample_count >= 3
        ]
        median = sorted(recent)[len(recent) // 2] / 1000.0 if recent else None
        timeouts = sum(w.timeout_count or 0 for w in r.windows)
        determined = sum(w.timeout_denominator_count or 0 for w in r.windows)
        counts = CheckOutcomeCounts()
        for result in r.outcomes:
            counts = counts.record(_kind(result))
        return RouteObservables(
            observed_outstanding=None if r.last_window is None else r.last_window.queue_depth,
            observed_median_latency_s=median,
            observed_timeout_rate=timeouts / determined if determined else None,
            late_resolution_hazard_per_s=hazard,
            ambiguous_backlog=backlog,
            current_check_rps=0.0,  # as the Stage 07 protocol: telemetry excludes checks
            check_outcomes=counts,
        )

    def state(self) -> Mapping[str, object]:
        return {
            "decisions": {"|".join(k): v for k, v in sorted(self.decisions.items())},
            "positive_budget_decisions": dict(sorted(self.budget_ticks.items())),
            "fallback_decisions": self.fallback_ticks,
            "ablation": self.config.ablation.value,
            "optimizer_digest": self.config.digest(),
        }


def _kind(result: str | None):
    from contracts.v1_1 import CheckResultKind

    return None if result is None else CheckResultKind(result)
