"""Deterministic fluid-queue simulator of synthetic payment routes (Stage 3B).

The simulator replays a Stage 3A ``Workload`` against its hidden ground truth and models how
status-check and re-payment traffic compete with new payments for finite route capacity.

Model (fixed timestep ``dt_s``, default 1 s; all rates in requests/s, times in seconds)
-----------------------------------------------------------------------------------
* Work: every request (payment, re-payment, same-key re-submission, status check, background
  check) is exactly one unit of work and keeps its identity. Service is fluid: in each interval
  a bottleneck can perform ``capacity x dt`` units, spent FIFO over age cohorts (one cohort per
  interval of arrival); fractional progress stays on the head request. Work is therefore never
  created or destroyed by rounding: every arrival is queued, completed or dropped.
* Capacity: the time-weighted average of the Stage 3A true capacity schedule over the half-open
  interval ``[t, t + dt)``. Routes listed together in ``bottleneck_groups`` share one FIFO queue
  whose capacity is the sum of their route capacities; otherwise each route is its own
  bottleneck (Stage 3A defines route capacities only, no separate switch or beneficiary tier).
* Order inside an interval: (1) drop cohorts older than ``stale_drop_age_s``; (2) enqueue, in
  time order, the status checks released at ``t``, scheduled customer actions and exogenous
  arrivals in ``[t, t + dt)``; (3) serve; (4) process remitter-side events with time
  ``< t + dt``. A request served in an interval completes at ``t + dt``; timing resolution is one
  timestep and no finer per-request precision is claimed.
* Payment lifecycle: served at ``s`` means the switch forwarded it; the beneficiary decides the
  outcome (credited or not, per ground truth) at ``s + latency``, and the response reaches the
  remitter at ``s + latency + confirmation delay`` unless it is lost. The client times out at
  ``sent + client_timeout_s``; no response by then makes the payment AMBIGUOUS, whatever the
  financial outcome. A later response resolves it. A payment dropped as stale before being
  forwarded was never processed. Nothing here reverses or refunds money.
* Status checks consume capacity. Served at ``s``, a check reports the beneficiary state at
  ``s``: AUTHORITATIVE_SUCCESS / AUTHORITATIVE_FAILURE once the beneficiary has decided, PENDING
  while the payment is queued or awaiting the beneficiary, UNKNOWN if it was dropped before the
  switch forwarded it (no record). The answer reaches the remitter after the route's median
  latency; if that is later than ``release + check_timeout_s`` the check timed out (wasted) and
  its answer is ignored.
* Customers: when a payment becomes ambiguous, the pre-drawn Stage 3A ``CustomerBehaviour`` may
  schedule one new payment (re-pay) or one same-key re-submission after the drawn delay, applied
  only if the payment is still unresolved when it falls due. Re-pays inherit the original's
  beneficiary behaviour and never trigger further re-pays. A re-submission is idempotent: it
  processes the payment only if the original never was.
* Policies: the engine releases checks only from external ``CheckDecision`` values and, unless a
  run is explicitly marked noncompliant (``enforce_timing_policy=False``), only for transactions
  the ``TimingPolicy`` allows (first-check delay, spacing, attempt cap, window; one check in
  flight per transaction).
* Observations: policies see only ``ControllerObservation`` (own-traffic ``RouteTelemetry`` and
  observed events). ``TickMetrics`` are ground truth and never reach a policy.

All randomness comes from the Stage 3A workload; the engine draws none and never reads the clock.
"""

import hashlib
import heapq
import json
import math
from collections import Counter, deque
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, StrictBool, model_validator

from contracts.v1_1 import SCHEMA_VERSION, CheckResultKind, FirstCheckReference, RouteTelemetry
from contracts.v1_1.models import Identifier, NonNegativeInt, PositiveFloat
from intelligence.data.generate_dataset import Workload
from intelligence.simulation.faults import CapacityFault
from intelligence.simulation.scenarios import HACKATHON_SCENARIO, Scenario


class SimulationError(Exception):
    """Base class for simulator errors."""


class SimulationFinishedError(SimulationError):
    """The simulation horizon has been reached; no further interval can be simulated."""


class SimulationAccountingError(SimulationError, RuntimeError):
    """Queue accounting no longer conserves work."""


class RequestClass(StrEnum):
    PAYMENT = "PAYMENT"
    REPAYMENT = "REPAYMENT"
    RESUBMISSION = "RESUBMISSION"
    STATUS_CHECK = "STATUS_CHECK"
    BACKGROUND_CHECK = "BACKGROUND_CHECK"


class SimulationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    dt_s: PositiveFloat = 1.0
    stale_drop_age_s: PositiveFloat | None = 30.0
    enforce_timing_policy: StrictBool = True
    bottleneck_groups: tuple[tuple[Identifier, ...], ...] = ()
    horizon_s: PositiveFloat | None = None  # default: the workload duration

    @model_validator(mode="after")
    def _groups(self) -> "SimulationConfig":
        members = [route for group in self.bottleneck_groups for route in group]
        if len(members) != len(set(members)):
            raise ValueError("a route can belong to only one bottleneck group")
        if any(len(group) < 2 for group in self.bottleneck_groups):
            raise ValueError("a bottleneck group needs at least two routes")
        return self


class CheckDecision(BaseModel):
    """Checks a policy asks to release on one route at the start of the next interval.

    Either ``count`` (the engine takes the oldest eligible ambiguous transactions) or explicit
    ``transaction_ids``; ineligible requests are denied, never forced through.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    route_id: Identifier
    count: NonNegativeInt = 0
    transaction_ids: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def _one_mode(self) -> "CheckDecision":
        if self.count and self.transaction_ids:
            raise ValueError("give either count or transaction_ids, not both")
        if len(set(self.transaction_ids)) != len(self.transaction_ids):
            raise ValueError("transaction_ids contains duplicates")
        return self


@dataclass(frozen=True)
class ObservedCheckResult:
    check_id: str
    transaction_id: str
    route_id: str
    result: CheckResultKind
    observed_at: datetime
    latency_ms: float


@dataclass(frozen=True)
class ObservedResolution:
    transaction_id: str
    route_id: str
    outcome: str  # SUCCESS or FAILURE, as observed by the remitter
    how: str  # TIMELY_RESPONSE, LATE_RESPONSE, CHECK or RESUBMISSION
    observed_at: datetime


@dataclass(frozen=True)
class ControllerObservation:
    """Everything a Tier 1 controller may know after an interval. No ground truth."""

    tick: int
    window_start: datetime
    window_end: datetime
    telemetry: tuple[RouteTelemetry, ...]
    sent_transaction_ids: tuple[str, ...]
    newly_ambiguous: tuple[str, ...]
    outcomes: tuple[ObservedResolution, ...]
    check_results: tuple[ObservedCheckResult, ...]
    check_timeouts: tuple[str, ...]
    denied_checks: tuple[str, ...]
    ambiguous_outstanding: tuple[tuple[str, tuple[str, ...]], ...]  # (route_id, txn ids)

    def outstanding(self, route_id: str) -> tuple[str, ...]:
        return dict(self.ambiguous_outstanding).get(route_id, ())


@dataclass(frozen=True)
class TickMetrics:
    """GROUND TRUTH per route and interval: simulator-side only, never given to a policy."""

    tick: int
    start_s: float
    route_id: str
    capacity_rps: float
    fault_active: bool
    payment_arrivals: int
    repayment_arrivals: int
    resubmission_arrivals: int
    checks_released: int
    checks_denied: int
    background_check_arrivals: int
    served: int
    dropped: int
    queue_depth: int
    bottleneck_queue_depth: int
    est_queue_delay_s: float | None
    timely_successes: int
    timely_failures: int
    late_responses: int
    new_ambiguous: int
    outstanding_ambiguous: int
    resolved: int
    check_results: int
    checks_nonfinal: int
    wasted_checks: int


@dataclass(frozen=True)
class SimulationResult:
    experiment_id: str
    run_id: str
    scenario_id: str
    workload_content_hash: str
    config: SimulationConfig
    timing_policy_enforced: bool
    ticks: int
    metrics: tuple[TickMetrics, ...]
    totals: tuple[tuple[str, int], ...]
    digest: str

    def total(self, name: str) -> int:
        return dict(self.totals)[name]


# ------------------------------------------------------------------ internal state


@dataclass(eq=False)
class _Payment:
    txn_id: str
    route_id: str
    kind: RequestClass
    sent_s: float
    deadline_s: float
    credited: bool
    latency_s: float
    response_lost: bool
    confirmation_delay_s: float
    would_repay: bool = False
    repay_delay_s: float = 0.0
    would_resubmit: bool = False
    resubmit_delay_s: float = 0.0
    processed_s: float | None = None  # when the beneficiary decided the outcome
    forwarded: bool = False
    dropped: bool = False
    awaiting: bool = True
    observed_outcome: str | None = None
    ambiguous_s: float | None = None
    resolved_s: float | None = None
    checks_counted: int = 0
    last_check_s: float | None = None
    check_in_flight: str | None = None
    customer_acted: bool = False

    @property
    def unresolved(self) -> bool:
        return self.ambiguous_s is not None and self.resolved_s is None


@dataclass(eq=False)
class _Check:
    check_id: str
    payment: _Payment
    release_s: float
    deadline_s: float
    observed: bool = False
    timed_out: bool = False


@dataclass(eq=False)
class _Resubmission:
    payment: _Payment
    sent_s: float
    deadline_s: float
    answered: bool = False


@dataclass
class _Bottleneck:
    name: str
    routes: tuple[str, ...]
    cohorts: deque = field(default_factory=deque)  # [tick, deque[(cls, obj, route)]]
    head_progress: float = 0.0
    queued_by_route: Counter = field(default_factory=Counter)


@dataclass
class _Acc:
    payment_arrivals: int = 0
    repayment_arrivals: int = 0
    resubmission_arrivals: int = 0
    checks_released: int = 0
    checks_denied: int = 0
    background_check_arrivals: int = 0
    served: int = 0
    dropped: int = 0
    timely_successes: int = 0
    timely_failures: int = 0
    late_responses: int = 0
    new_ambiguous: int = 0
    resolved: int = 0
    check_results: int = 0
    checks_nonfinal: int = 0
    wasted_checks: int = 0
    sent: int = 0
    payment_responses: int = 0
    check_responses: int = 0
    latencies_ms: list = field(default_factory=list)
    timeouts: int = 0


_PRIORITY = {
    "PAY_RESPONSE": 0,
    "RESUBMIT_RESPONSE": 0,
    "CHECK_RESULT": 1,
    "PAY_DEADLINE": 2,
    "RESUBMIT_DEADLINE": 2,
    "CHECK_DEADLINE": 3,
}


def _percentile(sorted_values: list[float], fraction: float) -> float:
    index = max(0, math.ceil(fraction * len(sorted_values)) - 1)
    return sorted_values[index]


class FluidSimulation:
    """Advance a Stage 3A workload one interval at a time under external check decisions."""

    def __init__(self, workload: Workload, config: SimulationConfig | None = None) -> None:
        if not isinstance(workload, Workload):
            raise TypeError("workload must be a Stage 3A Workload")
        self._config = config or SimulationConfig()
        if not isinstance(self._config, SimulationConfig):
            raise TypeError("config must be a SimulationConfig")
        manifest = workload.manifest
        scenario = manifest.config
        self._workload = workload
        self._policy = scenario.timing_policy
        dt = self._config.dt_s
        if dt > self._policy.client_timeout_s or dt > self._policy.check_timeout_s:
            raise ValueError("dt_s must not exceed the client or check timeout")
        self._epoch = scenario.epoch
        horizon = self._config.horizon_s or scenario.duration_s
        self._ticks_total = max(1, math.ceil(horizon / dt - 1e-9))
        self._routes = tuple(route.route_id for route in scenario.routes)
        self._check_latency_s = {
            route.route_id: route.beneficiary_median_latency_ms / 1000.0
            for route in scenario.routes
        }
        self._validate_capacity(workload)
        self._capacity = {route: [] for route in self._routes}
        for change in workload.ground_truth.capacity:
            self._capacity[change.route_id].append((change.offset_s, change.capacity_rps))
        for changes in self._capacity.values():
            changes.sort()
        self._faults = [
            (f.route_id, f.start_offset_s, f.end_offset_s) for f in workload.ground_truth.faults
        ]

        grouped = {route: group for group in self._config.bottleneck_groups for route in group}
        unknown = sorted(set(grouped) - set(self._routes))
        if unknown:
            raise ValueError(f"bottleneck_groups name unknown routes: {unknown}")
        self._bottlenecks: dict[str, _Bottleneck] = {}
        self._bottleneck_of: dict[str, _Bottleneck] = {}
        for route in self._routes:
            members = grouped.get(route, (route,))
            name = "+".join(members)
            if name not in self._bottlenecks:
                self._bottlenecks[name] = _Bottleneck(name, tuple(members))
            self._bottleneck_of[route] = self._bottlenecks[name]

        ids = [p.transaction_id for p in workload.exogenous.payments]
        if len(ids) != len(set(ids)):
            raise ValueError("workload contains duplicate payment transaction_ids")
        truth = {t.transaction_id: t for t in workload.ground_truth.beneficiary}
        behaviour = {b.transaction_id: b for b in workload.exogenous.customer_behaviour}
        if set(truth) != set(ids) or set(behaviour) != set(ids):
            raise ValueError("every payment needs exactly one truth and one behaviour record")
        self._truth, self._behaviour = truth, behaviour
        self._arrivals = sorted(
            workload.exogenous.payments, key=lambda p: (p.offset_s, p.transaction_id)
        )
        self._background = sorted(
            workload.exogenous.check_demands, key=lambda c: (c.offset_s, c.check_id)
        )
        for item in self._background:
            if item.route_id not in self._bottleneck_of:
                raise ValueError(f"check demand names unknown route {item.route_id}")
        self._next_arrival = 0
        self._next_background = 0

        self._tick = 0
        self._seq = 0
        self._events: list = []
        self._scheduled: list = []
        self._payments: dict[str, _Payment] = {}
        self._ambiguous_by_route: dict[str, dict[str, _Payment]] = {r: {} for r in self._routes}
        self._awaiting = Counter()
        self._arrived = Counter()
        self._completed = Counter()
        self._dropped_count = Counter()
        self._queued = Counter()
        self._resolution_how = Counter()
        self._metrics: list[TickMetrics] = []
        self._digest = hashlib.sha256(manifest.content_hash.encode())
        self._last_observation: ControllerObservation | None = None

    # -------------------------------------------------------------- public interface

    @property
    def done(self) -> bool:
        return self._tick >= self._ticks_total

    @property
    def tick(self) -> int:
        return self._tick

    @property
    def now(self) -> datetime:
        return self._at(self._tick * self._config.dt_s)

    @property
    def last_observation(self) -> ControllerObservation | None:
        return self._last_observation

    def step(self, decisions: Iterable[CheckDecision] = ()) -> ControllerObservation:
        """Simulate one interval; ``decisions`` release checks at its start."""

        if self.done:
            raise SimulationFinishedError("the simulation horizon has been reached")
        decisions = tuple(decisions)
        for decision in decisions:
            if not isinstance(decision, CheckDecision):
                raise TypeError("decisions must be CheckDecision instances")
            if decision.route_id not in self._ambiguous_by_route:
                raise ValueError(f"decision names unknown route {decision.route_id}")
        dt = self._config.dt_s
        start, end = self._tick * dt, (self._tick + 1) * dt
        acc = {route: _Acc() for route in self._routes}
        obs = {"sent": [], "ambiguous": [], "outcomes": [], "checks": [], "timeouts": []}
        denied: list[str] = []

        self._drop_stale(start, acc)
        pending: list[tuple[float, int, tuple]] = []
        self._release_checks(decisions, start, acc, pending, denied)
        self._due_customer_actions(start, end, acc, pending, obs)
        self._exogenous_arrivals(end, acc, pending, obs)
        pending.sort(key=lambda item: (item[0], item[1]))
        for _, _, item in pending:
            self._enqueue(item, self._tick)
        capacities = {route: self._average_capacity(route, start, end) for route in self._routes}
        for bottleneck in self._bottlenecks.values():
            capacity = sum(capacities[r] for r in bottleneck.routes)
            self._serve(bottleneck, capacity * dt, end, acc)
        self._process_events(end, acc, obs)
        self._check_counters()

        observation = self._observation(start, end, acc, obs, denied)
        self._record_metrics(start, capacities, acc)
        self._digest.update(self._observation_json(observation).encode())
        self._tick += 1
        self._last_observation = observation
        return observation

    def run(
        self,
        policy: Callable[[ControllerObservation | None], Iterable[CheckDecision]] | None = None,
    ) -> SimulationResult:
        """Run to the horizon. ``policy`` sees only the previous ControllerObservation."""

        while not self.done:
            decisions = policy(self._last_observation) if policy is not None else ()
            self.step(decisions)
        return self.result()

    def result(self) -> SimulationResult:
        manifest = self._workload.manifest
        digest = self._digest.copy()
        for metric in self._metrics:
            digest.update(json.dumps(asdict(metric), sort_keys=True).encode())
        return SimulationResult(
            experiment_id=manifest.experiment_id,
            run_id=manifest.run_id,
            scenario_id=manifest.scenario_id,
            workload_content_hash=manifest.content_hash,
            config=self._config,
            timing_policy_enforced=self._config.enforce_timing_policy,
            ticks=self._tick,
            metrics=tuple(self._metrics),
            totals=tuple(sorted(self._totals().items())),
            digest=digest.hexdigest(),
        )

    def metrics_until(self, tick: int) -> tuple[TickMetrics, ...]:
        """Ground-truth metrics for intervals before ``tick`` (never beyond what was simulated)."""

        if tick > self._tick:
            raise SimulationFinishedError(f"tick {tick} has not been simulated yet")
        return tuple(m for m in self._metrics if m.tick < tick)

    def check_invariants(self) -> None:
        """Full scan: every arrival is queued, completed or dropped, per request class."""

        scanned = Counter()
        for bottleneck in self._bottlenecks.values():
            for _, items in bottleneck.cohorts:
                for cls, _, _ in items:
                    scanned[cls] += 1
        for cls in RequestClass:
            if scanned[cls] != self._queued[cls]:
                raise SimulationAccountingError(f"{cls}: queue scan disagrees with counter")
        self._check_counters()

    # -------------------------------------------------------------- interval phases

    def _drop_stale(self, start: float, acc: dict[str, _Acc]) -> None:
        age_limit = self._config.stale_drop_age_s
        if age_limit is None:
            return
        dt = self._config.dt_s
        for bottleneck in self._bottlenecks.values():
            while bottleneck.cohorts and start - bottleneck.cohorts[0][0] * dt > age_limit:
                _, items = bottleneck.cohorts.popleft()
                bottleneck.head_progress = 0.0
                for cls, obj, route in items:
                    self._dequeue(bottleneck, cls, route)
                    self._dropped_count[cls] += 1
                    acc[route].dropped += 1
                    if cls in (RequestClass.PAYMENT, RequestClass.REPAYMENT):
                        if obj.processed_s is None:
                            obj.dropped = True
                    elif (
                        cls is RequestClass.STATUS_CHECK
                        and not obj.observed
                        and not (obj.timed_out)
                    ):
                        self._time_out_check(obj, acc)

    def _release_checks(self, decisions, start, acc, pending, denied) -> None:
        for decision in decisions:
            ambiguous = self._ambiguous_by_route[decision.route_id]
            denied_before = len(denied)
            if decision.transaction_ids:
                chosen = []
                for txn in decision.transaction_ids:
                    payment = ambiguous.get(txn)
                    if payment is not None and self._eligible(payment, start):
                        chosen.append(payment)
                    else:
                        denied.append(txn)
            else:
                chosen = []
                for payment in ambiguous.values():
                    if len(chosen) >= decision.count:
                        break
                    if self._eligible(payment, start):
                        chosen.append(payment)
                shortfall = decision.count - len(chosen)
                if shortfall:
                    denied.append(f"{decision.route_id}:count-shortfall:{shortfall}")
            if decision.transaction_ids:
                acc[decision.route_id].checks_denied += len(denied) - denied_before
            else:
                acc[decision.route_id].checks_denied += decision.count - len(chosen)
            for payment in chosen:
                payment.checks_counted += 1
                check = _Check(
                    check_id=f"CK-{payment.txn_id}-{payment.checks_counted}",
                    payment=payment,
                    release_s=start,
                    deadline_s=start + self._policy.check_timeout_s,
                )
                payment.last_check_s = start
                payment.check_in_flight = check.check_id
                self._push(check.deadline_s, "CHECK_DEADLINE", check)
                self._awaiting[payment.route_id] += 1
                acc[payment.route_id].checks_released += 1
                acc[payment.route_id].sent += 1
                pending.append((start, 0, (RequestClass.STATUS_CHECK, check, payment.route_id)))

    def _eligible(self, payment: _Payment, now: float) -> bool:
        if not payment.unresolved or payment.check_in_flight is not None:
            return False
        if not self._config.enforce_timing_policy:
            return True
        policy = self._policy
        if payment.checks_counted >= policy.max_counted_checks:
            return False
        if policy.first_check_reference is FirstCheckReference.INITIATED:
            reference = payment.sent_s
        else:
            reference = payment.ambiguous_s
        if now < reference + policy.first_check_delay_s or now > reference + policy.check_window_s:
            return False
        return payment.last_check_s is None or now >= payment.last_check_s + (
            policy.min_check_spacing_s
        )

    def _due_customer_actions(self, start, end, acc, pending, obs) -> None:
        while self._scheduled and self._scheduled[0][0] < end:
            due, _, kind, payment = heapq.heappop(self._scheduled)
            if not payment.unresolved:  # resolved by the start of the interval: no action
                continue
            if kind == "REPAY":
                repay = _Payment(
                    txn_id=f"RP-{payment.txn_id}",
                    route_id=payment.route_id,
                    kind=RequestClass.REPAYMENT,
                    sent_s=due,
                    deadline_s=due + self._policy.client_timeout_s,
                    credited=payment.credited,
                    latency_s=payment.latency_s,
                    response_lost=payment.response_lost,
                    confirmation_delay_s=payment.confirmation_delay_s,
                )
                self._payments[repay.txn_id] = repay
                self._push(repay.deadline_s, "PAY_DEADLINE", repay)
                self._awaiting[repay.route_id] += 1
                acc[repay.route_id].repayment_arrivals += 1
                acc[repay.route_id].sent += 1
                obs["sent"].append(repay.txn_id)
                pending.append((due, 1, (RequestClass.REPAYMENT, repay, repay.route_id)))
            else:
                resubmission = _Resubmission(payment, due, due + self._policy.client_timeout_s)
                self._push(resubmission.deadline_s, "RESUBMIT_DEADLINE", resubmission)
                self._awaiting[payment.route_id] += 1
                acc[payment.route_id].resubmission_arrivals += 1
                acc[payment.route_id].sent += 1
                pending.append(
                    (due, 1, (RequestClass.RESUBMISSION, resubmission, payment.route_id))
                )

    def _exogenous_arrivals(self, end, acc, pending, obs) -> None:
        while self._next_arrival < len(self._arrivals):
            arrival = self._arrivals[self._next_arrival]
            if arrival.offset_s >= end:
                break
            self._next_arrival += 1
            truth = self._truth[arrival.transaction_id]
            behaviour = self._behaviour[arrival.transaction_id]
            payment = _Payment(
                txn_id=arrival.transaction_id,
                route_id=arrival.route_id,
                kind=RequestClass.PAYMENT,
                sent_s=arrival.offset_s,
                deadline_s=arrival.offset_s + self._policy.client_timeout_s,
                credited=truth.credited,
                latency_s=truth.beneficiary_latency_ms / 1000.0,
                response_lost=truth.response_lost,
                confirmation_delay_s=truth.confirmation_delay_s,
                would_repay=behaviour.would_repay,
                repay_delay_s=behaviour.repay_delay_s,
                would_resubmit=behaviour.would_resubmit_same_key,
                resubmit_delay_s=behaviour.resubmit_delay_s,
            )
            self._payments[payment.txn_id] = payment
            self._push(payment.deadline_s, "PAY_DEADLINE", payment)
            self._awaiting[payment.route_id] += 1
            acc[payment.route_id].payment_arrivals += 1
            acc[payment.route_id].sent += 1
            obs["sent"].append(payment.txn_id)
            pending.append((arrival.offset_s, 2, (RequestClass.PAYMENT, payment, payment.route_id)))
        while self._next_background < len(self._background):
            demand = self._background[self._next_background]
            if demand.offset_s >= end:
                break
            self._next_background += 1
            acc[demand.route_id].background_check_arrivals += 1
            pending.append(
                (demand.offset_s, 3, (RequestClass.BACKGROUND_CHECK, demand, demand.route_id))
            )

    def _serve(self, bottleneck: _Bottleneck, budget: float, end: float, acc) -> None:
        while bottleneck.cohorts and budget > 0.0:
            items = bottleneck.cohorts[0][1]
            need = 1.0 - bottleneck.head_progress
            if budget + 1e-12 < need:
                bottleneck.head_progress += budget
                return
            budget -= need
            bottleneck.head_progress = 0.0
            cls, obj, route = items.popleft()
            if not items:
                bottleneck.cohorts.popleft()
            self._dequeue(bottleneck, cls, route)
            self._completed[cls] += 1
            acc[route].served += 1
            self._complete(cls, obj, end)

    def _complete(self, cls: RequestClass, obj, end: float) -> None:
        # Switch service forwards a payment at ``end``; the beneficiary decides its outcome after
        # its intrinsic latency (``processed_s``) and the response follows any confirmation delay.
        if cls in (RequestClass.PAYMENT, RequestClass.REPAYMENT):
            obj.forwarded = True
            if obj.processed_s is None:
                obj.processed_s = end + obj.latency_s
            if not obj.response_lost:
                arrival = end + obj.latency_s + obj.confirmation_delay_s
                self._push(arrival, "PAY_RESPONSE", obj)
        elif cls is RequestClass.RESUBMISSION:
            payment = obj.payment
            payment.forwarded = True
            if payment.processed_s is None:  # idempotent: only the first processing counts
                payment.processed_s = end + payment.latency_s
            if not payment.response_lost:
                arrival = end + payment.latency_s + payment.confirmation_delay_s
                self._push(arrival, "RESUBMIT_RESPONSE", obj)
        elif cls is RequestClass.STATUS_CHECK and not obj.timed_out:
            payment = obj.payment
            if payment.processed_s is not None and payment.processed_s <= end:
                result = (
                    CheckResultKind.AUTHORITATIVE_SUCCESS
                    if payment.credited
                    else CheckResultKind.AUTHORITATIVE_FAILURE
                )
            elif payment.dropped and not payment.forwarded:
                result = CheckResultKind.UNKNOWN  # the switch never forwarded it: no record
            else:
                result = CheckResultKind.PENDING
            answer_at = end + self._check_latency_s[payment.route_id]
            if answer_at <= obj.deadline_s:
                self._push(answer_at, "CHECK_RESULT", (obj, result))

    def _process_events(self, end: float, acc, obs) -> None:
        while self._events and self._events[0][0] < end:
            at, _, _, kind, payload = heapq.heappop(self._events)
            handler = getattr(self, f"_on_{kind.lower()}")
            handler(at, payload, acc, obs)

    # -------------------------------------------------------------- remitter-side events

    def _on_pay_response(self, at: float, payment: _Payment, acc, obs) -> None:
        route = payment.route_id
        acc[route].payment_responses += 1
        acc[route].latencies_ms.append((at - payment.sent_s) * 1000.0)
        outcome = "SUCCESS" if payment.credited else "FAILURE"
        if payment.awaiting:  # within the client timeout
            payment.awaiting = False
            self._awaiting[route] -= 1
            payment.observed_outcome = outcome
            if payment.credited:
                acc[route].timely_successes += 1
            else:
                acc[route].timely_failures += 1
            obs["outcomes"].append(
                ObservedResolution(payment.txn_id, route, outcome, "TIMELY_RESPONSE", self._at(at))
            )
        else:
            acc[route].late_responses += 1
            if payment.unresolved:
                self._resolve(payment, at, "LATE_RESPONSE", acc, obs)

    def _on_pay_deadline(self, at: float, payment: _Payment, acc, obs) -> None:
        if not payment.awaiting:
            return
        payment.awaiting = False
        route = payment.route_id
        self._awaiting[route] -= 1
        acc[route].timeouts += 1
        acc[route].new_ambiguous += 1
        payment.ambiguous_s = at
        self._ambiguous_by_route[route][payment.txn_id] = payment
        obs["ambiguous"].append(payment.txn_id)
        if payment.kind is RequestClass.PAYMENT and not payment.customer_acted:
            payment.customer_acted = True
            if payment.would_repay:
                self._schedule(at + payment.repay_delay_s, "REPAY", payment)
            if payment.would_resubmit:
                self._schedule(at + payment.resubmit_delay_s, "RESUBMIT", payment)

    def _on_resubmit_response(self, at: float, resubmission: _Resubmission, acc, obs) -> None:
        payment = resubmission.payment
        route = payment.route_id
        acc[route].payment_responses += 1
        acc[route].latencies_ms.append((at - resubmission.sent_s) * 1000.0)
        if resubmission.answered:
            return
        resubmission.answered = True
        self._awaiting[route] -= 1
        if payment.unresolved:
            self._resolve(payment, at, "RESUBMISSION", acc, obs)

    def _on_resubmit_deadline(self, at: float, resubmission: _Resubmission, acc, obs) -> None:
        if resubmission.answered:
            return
        resubmission.answered = True
        self._awaiting[resubmission.payment.route_id] -= 1
        acc[resubmission.payment.route_id].timeouts += 1

    def _on_check_result(self, at: float, payload, acc, obs) -> None:
        check, result = payload
        if check.observed or check.timed_out:
            return
        check.observed = True
        payment = check.payment
        route = payment.route_id
        if payment.check_in_flight == check.check_id:
            payment.check_in_flight = None
        self._awaiting[route] -= 1
        acc[route].check_results += 1
        acc[route].check_responses += 1
        latency_ms = (at - check.release_s) * 1000.0
        acc[route].latencies_ms.append(latency_ms)
        obs["checks"].append(
            ObservedCheckResult(
                check.check_id, payment.txn_id, route, result, self._at(at), latency_ms
            )
        )
        if result in (
            CheckResultKind.AUTHORITATIVE_SUCCESS,
            CheckResultKind.AUTHORITATIVE_FAILURE,
        ):
            if payment.unresolved:
                self._resolve(payment, at, "CHECK", acc, obs)
        else:
            acc[route].checks_nonfinal += 1

    def _on_check_deadline(self, at: float, check: _Check, acc, obs) -> None:
        if check.observed or check.timed_out:
            return
        self._time_out_check(check, acc)
        obs["timeouts"].append(check.check_id)

    def _time_out_check(self, check: _Check, acc) -> None:
        check.timed_out = True
        payment = check.payment
        if payment.check_in_flight == check.check_id:
            payment.check_in_flight = None
        self._awaiting[payment.route_id] -= 1
        acc[payment.route_id].timeouts += 1
        acc[payment.route_id].wasted_checks += 1

    def _resolve(self, payment: _Payment, at: float, how: str, acc, obs) -> None:
        payment.resolved_s = at
        payment.observed_outcome = "SUCCESS" if payment.credited else "FAILURE"
        self._ambiguous_by_route[payment.route_id].pop(payment.txn_id, None)
        self._resolution_how[how] += 1
        acc[payment.route_id].resolved += 1
        obs["outcomes"].append(
            ObservedResolution(
                payment.txn_id, payment.route_id, payment.observed_outcome, how, self._at(at)
            )
        )

    # -------------------------------------------------------------- helpers

    def _at(self, offset_s: float) -> datetime:
        return self._epoch + timedelta(seconds=offset_s)

    def _push(self, at: float, kind: str, payload) -> None:
        self._seq += 1
        heapq.heappush(self._events, (at, _PRIORITY[kind], self._seq, kind, payload))

    def _schedule(self, at: float, kind: str, payment: _Payment) -> None:
        self._seq += 1
        heapq.heappush(self._scheduled, (at, self._seq, kind, payment))

    def _enqueue(self, item: tuple, tick: int) -> None:
        cls, _, route = item
        bottleneck = self._bottleneck_of[route]
        if not bottleneck.cohorts or bottleneck.cohorts[-1][0] != tick:
            bottleneck.cohorts.append([tick, deque()])
        bottleneck.cohorts[-1][1].append(item)
        bottleneck.queued_by_route[route] += 1
        self._queued[cls] += 1
        self._arrived[cls] += 1

    def _dequeue(self, bottleneck: _Bottleneck, cls: RequestClass, route: str) -> None:
        bottleneck.queued_by_route[route] -= 1
        self._queued[cls] -= 1

    def _check_counters(self) -> None:
        for cls in RequestClass:
            if (
                self._arrived[cls]
                != self._completed[cls] + self._dropped_count[cls] + (self._queued[cls])
                or min(self._queued[cls], self._completed[cls], self._dropped_count[cls]) < 0
            ):
                raise SimulationAccountingError(f"{cls}: arrivals are not conserved")

    @staticmethod
    def _validate_capacity(workload: Workload) -> None:
        for change in workload.ground_truth.capacity:
            value = change.capacity_rps
            if not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"invalid capacity {value!r} on route {change.route_id}")

    def _average_capacity(self, route: str, start: float, end: float) -> float:
        changes = self._capacity[route]
        if not changes:
            return 0.0
        total, current = 0.0, changes[0][1]
        cursor = start
        for offset, value in changes:
            if offset <= start:
                current = value
                continue
            if offset >= end:
                break
            total += current * (offset - cursor)
            cursor, current = offset, value
        total += current * (end - cursor)
        return total / (end - start)

    def _fault_active(self, route: str, at: float) -> bool:
        return any(r == route and s <= at < e for r, s, e in self._faults)

    def _observation(self, start, end, acc, obs, denied) -> ControllerObservation:
        dt = self._config.dt_s
        telemetry = []
        for route in self._routes:
            a = acc[route]
            latencies = sorted(a.latencies_ms)
            determined = a.timeouts + a.payment_responses + a.check_responses
            telemetry.append(
                RouteTelemetry(
                    schema_version=SCHEMA_VERSION,
                    experiment_id=self._workload.manifest.experiment_id,
                    route_id=route,
                    source_id=f"sim-remitter-{route}",
                    observation_scope="REMITTER_OWN_TRAFFIC",
                    sequence=self._tick,
                    observation_window_start=self._at(start),
                    observation_window_end=self._at(end),
                    observation_timestamp=self._at(end),
                    payment_rps=(
                        a.payment_arrivals + a.repayment_arrivals + a.resubmission_arrivals
                    )
                    / dt,
                    check_rps=a.checks_released / dt,
                    observed_throughput_rps=(a.payment_responses + a.check_responses) / dt,
                    completed_payment_rps=a.payment_responses / dt,
                    completed_check_rps=a.check_responses / dt,
                    p50_latency_ms=_percentile(latencies, 0.5) if latencies else None,
                    p95_latency_ms=_percentile(latencies, 0.95) if latencies else None,
                    latency_sample_count=len(latencies),
                    timeout_rate=a.timeouts / determined if determined else None,
                    timeout_count=a.timeouts,
                    timeout_denominator_count=determined,
                    queue_depth=self._awaiting[route],
                )
            )
        return ControllerObservation(
            tick=self._tick,
            window_start=self._at(start),
            window_end=self._at(end),
            telemetry=tuple(telemetry),
            sent_transaction_ids=tuple(obs["sent"]),
            newly_ambiguous=tuple(obs["ambiguous"]),
            outcomes=tuple(obs["outcomes"]),
            check_results=tuple(obs["checks"]),
            check_timeouts=tuple(obs["timeouts"]),
            denied_checks=tuple(denied),
            ambiguous_outstanding=tuple(
                (route, tuple(self._ambiguous_by_route[route])) for route in self._routes
            ),
        )

    @staticmethod
    def _observation_json(observation: ControllerObservation) -> str:
        data = asdict(observation)
        data["telemetry"] = [t.model_dump(mode="json") for t in observation.telemetry]
        return json.dumps(data, default=str, sort_keys=True)

    def _record_metrics(self, start: float, capacities: dict[str, float], acc) -> None:
        for route in self._routes:
            a = acc[route]
            bottleneck = self._bottleneck_of[route]
            group_capacity = sum(capacities[r] for r in bottleneck.routes)
            group_queue = sum(bottleneck.queued_by_route.values())
            self._metrics.append(
                TickMetrics(
                    tick=self._tick,
                    start_s=start,
                    route_id=route,
                    capacity_rps=capacities[route],
                    fault_active=self._fault_active(route, start),
                    payment_arrivals=a.payment_arrivals,
                    repayment_arrivals=a.repayment_arrivals,
                    resubmission_arrivals=a.resubmission_arrivals,
                    checks_released=a.checks_released,
                    checks_denied=a.checks_denied,
                    background_check_arrivals=a.background_check_arrivals,
                    served=a.served,
                    dropped=a.dropped,
                    queue_depth=bottleneck.queued_by_route[route],
                    bottleneck_queue_depth=group_queue,
                    est_queue_delay_s=group_queue / group_capacity if group_capacity > 0 else None,
                    timely_successes=a.timely_successes,
                    timely_failures=a.timely_failures,
                    late_responses=a.late_responses,
                    new_ambiguous=a.new_ambiguous,
                    outstanding_ambiguous=len(self._ambiguous_by_route[route]),
                    resolved=a.resolved,
                    check_results=a.check_results,
                    checks_nonfinal=a.checks_nonfinal,
                    wasted_checks=a.wasted_checks,
                )
            )

    def _totals(self) -> dict[str, int]:
        sums = Counter()
        for metric in self._metrics:
            for name, value in asdict(metric).items():
                if isinstance(value, int) and not isinstance(value, bool) and name != "tick":
                    sums[name] += value
        totals = {
            name: sums[name]
            for name in (
                "payment_arrivals",
                "repayment_arrivals",
                "resubmission_arrivals",
                "checks_released",
                "checks_denied",
                "background_check_arrivals",
                "served",
                "dropped",
                "timely_successes",
                "timely_failures",
                "late_responses",
                "new_ambiguous",
                "resolved",
                "check_results",
                "checks_nonfinal",
                "wasted_checks",
            )
        }
        totals["unresolved_at_end"] = sum(len(v) for v in self._ambiguous_by_route.values())
        totals["queued_at_end"] = sum(self._queued.values())
        for how in ("LATE_RESPONSE", "CHECK", "RESUBMISSION"):
            totals[f"resolved_by_{how.lower()}"] = self._resolution_how[how]
        for cls in RequestClass:
            totals[f"arrived_{cls.lower()}"] = self._arrived[cls]
            totals[f"completed_{cls.lower()}"] = self._completed[cls]
            totals[f"dropped_{cls.lower()}"] = self._dropped_count[cls]
        return totals


def simulate(
    workload: Workload,
    config: SimulationConfig | None = None,
    policy: Callable[[ControllerObservation | None], Iterable[CheckDecision]] | None = None,
) -> SimulationResult:
    """Convenience wrapper: run ``workload`` to the horizon under ``policy``."""

    return FluidSimulation(workload, config).run(policy)


# --------------------------------------------------------------------- legacy demonstration


@dataclass(frozen=True)
class Tick:
    second: int
    capacity_rps: float
    check_rps: float
    ambiguous_total: float


def run(scenario: Scenario = HACKATHON_SCENARIO) -> list[Tick]:
    fault = CapacityFault()
    ambiguous = 0.0
    check_rps = 0.0
    ticks = []
    for second in range(scenario.duration_seconds):
        capacity = scenario.route_capacity_rps
        if scenario.fault_at_second is not None and second >= scenario.fault_at_second:
            capacity = fault.apply(capacity)
        payment_capacity = max(0.0, capacity - check_rps)
        new_ambiguous = max(0.0, scenario.payment_rps - payment_capacity)
        resolved = min(ambiguous, check_rps)
        ambiguous = max(0.0, ambiguous + new_ambiguous - resolved)
        # Conservative standalone controller: never use more than 10% of capacity.
        check_rps = min(ambiguous, capacity * 0.10)
        ticks.append(Tick(second, capacity, check_rps, ambiguous))
    return ticks


def main() -> None:
    ticks = run()
    print("second capacity_rps check_rps ambiguous")
    for tick in ticks:
        print(
            f"{tick.second:>6} {tick.capacity_rps:>12.1f} "
            f"{tick.check_rps:>9.1f} {tick.ambiguous_total:>9.1f}"
        )


if __name__ == "__main__":
    main()
