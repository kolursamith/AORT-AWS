"""Independent per-request discrete-event model (SimPy) of a Stage 3A workload (Stage 04).

This model exists to validate the Stage 3B fluid simulator, so it shares the *inputs*
(exogenous arrivals, pre-drawn customer intent, ground-truth capacity and beneficiary
behaviour, timing policy) but none of the queue mechanics. It does not import the fluid engine.

Modelling choices that differ on purpose from the fluid model
-------------------------------------------------------------
* Every request is an individual entity served by a single work-conserving server per
  bottleneck, strictly FIFO in arrival order (ties: arrival time, then a stable class order,
  then identifier). The server's speed is the true capacity schedule, piecewise constant on
  half-open intervals ``[change_i, change_{i+1})``; a request of work ``w`` that starts at ``t``
  completes at the exact instant when the integral of capacity from ``t`` reaches ``w``. There
  is no timestep: completions, deadlines and responses happen at exact continuous times.
* Work per request is 1 unit (``DETERMINISTIC``) or drawn from Exp(1) (``EXPONENTIAL``) using a
  per-bottleneck stream seeded from ``sha256("aorts-des|<seed>|<bottleneck>")``.
* A request still waiting (not in service) ``stale_drop_age_s`` after it was enqueued is dropped
  at exactly that instant. A request in service is never dropped.
* Outcomes that depend on two events at the same instant are decided from their known times,
  not from event ordering: a payment response at ``t <= deadline`` is timely; a check answer at
  ``t <= release + check_timeout_s`` counts; a customer action due at ``t`` is skipped when the
  payment's resolution time is ``<= t``.
* Status checks come only from an externally supplied schedule of ``CheckRelease`` entries
  ("release up to ``count`` checks on ``route_id`` at ``time_s``"). There is no recovery policy
  here. Selection is the oldest-ambiguous-first eligible payments; eligibility follows the
  ``TimingPolicy`` unless the run is explicitly marked noncompliant. A release at ``t`` sees
  ambiguity established strictly before ``t``.
* All counts are reconstructed after the run from per-request time logs, with half-open bins
  ``[k*dt, (k+1)*dt)``; queue depth at ``t`` counts requests enqueued before ``t`` and not yet
  completed or dropped before ``t`` (in-service requests included).

Closed-loop control (Stage 05): an optional ``controller`` callback is called every
``decision_interval_s`` with a causal ``DesObservation`` covering ``[previous decision, now)``
(only events strictly before ``now``) and the acceptance feedback of its previous requests. It
returns ``CheckRequest`` values naming transactions; they pass the same eligibility gate as the
schedule path. Without a controller the model runs exactly as before.

Ground truth (capacity, credit, latency, intent) drives the model but is reported only in
``DesResult.intervals`` and ``totals``, which are evaluation data. ``DesResult.observations``
holds only what a remitter could see and never carries truth.
"""

import hashlib
import heapq
import json
import math
import random
from bisect import bisect_right
from collections import Counter, deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum

import simpy
from pydantic import BaseModel, ConfigDict, NonNegativeInt, PositiveFloat, StrictBool

from contracts.v1_1 import CheckResultKind, FirstCheckReference
from intelligence.data.generate_dataset import Workload

DES_MODEL_VERSION = "aorts-des/1"
_INF = math.inf


class ServiceDistribution(StrEnum):
    DETERMINISTIC = "DETERMINISTIC"
    EXPONENTIAL = "EXPONENTIAL"


class DesClass(StrEnum):
    PAYMENT = "payment"
    REPAYMENT = "repayment"
    RESUBMISSION = "resubmission"
    STATUS_CHECK = "status_check"
    BACKGROUND_CHECK = "background_check"


# Stable order for requests that arrive at the same instant.
_CLASS_ORDER = {
    DesClass.STATUS_CHECK: 0,
    DesClass.REPAYMENT: 1,
    DesClass.RESUBMISSION: 1,
    DesClass.PAYMENT: 2,
    DesClass.BACKGROUND_CHECK: 3,
}


class DesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    bin_s: PositiveFloat = 1.0
    stale_drop_age_s: PositiveFloat | None = 30.0
    service: ServiceDistribution = ServiceDistribution.DETERMINISTIC
    service_seed: NonNegativeInt = 0
    enforce_timing_policy: StrictBool = True
    bottleneck_groups: tuple[tuple[str, ...], ...] = ()
    horizon_s: PositiveFloat | None = None


@dataclass(frozen=True)
class CheckRelease:
    """External schedule entry: release up to ``count`` eligible checks on a route."""

    time_s: float
    route_id: str
    count: int

    def __post_init__(self) -> None:
        if not math.isfinite(self.time_s) or self.time_s < 0:
            raise ValueError("CheckRelease.time_s must be finite and >= 0")
        if not isinstance(self.count, int) or self.count < 0:
            raise ValueError("CheckRelease.count must be a non-negative int")


@dataclass(frozen=True)
class CheckRequest:
    """Closed-loop request: check these ambiguous transactions on one route now."""

    route_id: str
    transaction_ids: tuple[str, ...]


@dataclass(frozen=True)
class ReleaseFeedback:
    """What happened to the previous decision's requests (known at the decision instant)."""

    decided_at_s: float
    released: tuple[tuple[str, str], ...]  # (transaction_id, check_id)
    refused: tuple[tuple[str, str], ...]  # (transaction_id, reason)


Controller = Callable[[float, "DesObservation", ReleaseFeedback | None], Iterable[CheckRequest]]


class DesError(ValueError):
    pass


@dataclass
class _Request:
    cls: DesClass
    ident: str
    route: str
    enqueued_s: float
    work: float = 1.0
    started_s: float | None = None
    planned_finish_s: float | None = None
    finished_s: float | None = None
    dropped_s: float | None = None
    payment: "_Payment | None" = None
    check: "_Check | None" = None
    completed: bool = False

    def ended_by(self, t: float) -> bool:
        end = self.finished_s if self.finished_s is not None else self.dropped_s
        return end is not None and end < t


@dataclass
class _Payment:
    txn: str
    route: str
    kind: DesClass
    sent_s: float
    deadline_s: float
    credited: bool
    latency_s: float
    lost: bool
    confirm_s: float
    would_repay: bool = False
    repay_delay_s: float = 0.0
    would_resubmit: bool = False
    resubmit_delay_s: float = 0.0
    request: _Request | None = None
    forwarded_s: float | None = None  # first switch completion (payment or resubmission)
    processed_s: float | None = None  # beneficiary decision instant
    response_s: float | None = None  # earliest response instant from the original request
    ambiguous_s: float | None = None
    resolved_s: float | None = None
    resolved_how: str | None = None
    checks_counted: int = 0
    last_check_s: float | None = None
    check_in_flight: "_Check | None" = None
    customer_acted: bool = False

    def resolved_by(self, t: float) -> bool:
        return self.resolved_s is not None and self.resolved_s <= t

    def dropped_unforwarded_by(self, t: float) -> bool:
        req = self.request
        return (
            req is not None
            and req.dropped_s is not None
            and req.dropped_s <= t
            and self.forwarded_s is None
        )


@dataclass
class _Check:
    check_id: str
    payment: _Payment
    release_s: float
    deadline_s: float
    answer_s: float | None = None
    result: CheckResultKind | None = None
    timed_out: bool = False
    request: _Request | None = None


@dataclass(frozen=True)
class DesObservation:
    """What a remitter could observe in one bin. No ground truth."""

    bin_index: int
    start_s: float
    end_s: float
    newly_ambiguous: tuple[str, ...]
    resolutions: tuple[tuple[str, str, str, float], ...]  # txn, observed outcome, how, time
    check_results: tuple[tuple[str, str, str, float], ...]  # check, txn, result, time
    check_timeouts: tuple[str, ...]
    ambiguous_outstanding: tuple[tuple[str, tuple[str, ...]], ...]


@dataclass(frozen=True)
class DesInterval:
    """Ground-truth metrics for one route and bin (evaluation data only)."""

    bin_index: int
    start_s: float
    route_id: str
    payment_arrivals: int
    repayment_arrivals: int
    resubmission_arrivals: int
    checks_released: int
    background_check_arrivals: int
    served: int
    dropped: int
    queue_depth: int
    bottleneck_queue_depth: int
    new_ambiguous: int
    timely_successes: int
    timely_failures: int
    late_responses: int
    resolved: int
    check_results: int
    wasted_checks: int


@dataclass(frozen=True)
class TransactionTrace:
    """Per-payment timing for model comparison (evaluation data only)."""

    txn: str
    route_id: str
    kind: str
    sent_s: float
    response_s: float | None
    ambiguous_s: float | None
    resolved_s: float | None
    resolved_how: str | None


@dataclass(frozen=True)
class DesResult:
    model_version: str
    experiment_id: str
    run_id: str
    scenario_id: str
    workload_content_hash: str
    config: DesConfig
    horizon_s: float
    bins: int
    intervals: tuple[DesInterval, ...]
    observations: tuple[DesObservation, ...]
    traces: tuple[TransactionTrace, ...]
    totals: tuple[tuple[str, int], ...]
    digest: str

    def total(self, name: str) -> int:
        return dict(self.totals)[name]


@dataclass
class _Bottleneck:
    name: str
    routes: tuple[str, ...]
    breakpoints: list[float]
    capacities: list[float]
    rng: random.Random
    waiting: deque = field(default_factory=deque)
    in_service: _Request | None = None
    wakeup: object = None

    def capacity_at(self, t: float) -> float:
        return self.capacities[bisect_right(self.breakpoints, t) - 1]

    def next_positive(self, t: float) -> float | None:
        """First capacity change after ``t`` that restores a positive capacity."""

        for index in range(bisect_right(self.breakpoints, t), len(self.breakpoints)):
            if self.capacities[index] > 0:
                return self.breakpoints[index]
        return None

    def finish_time(self, start: float, work: float) -> float:
        """Exact completion instant of ``work`` units started at ``start``."""

        t, remaining = start, work
        index = bisect_right(self.breakpoints, t) - 1
        while True:
            capacity = self.capacities[index]
            seg_end = self.breakpoints[index + 1] if index + 1 < len(self.breakpoints) else _INF
            if capacity > 0:
                doable = capacity * (seg_end - t)
                if remaining <= doable + 1e-12:
                    return t + remaining / capacity
                remaining -= doable
            if seg_end == _INF:
                return _INF
            t, index = seg_end, index + 1


class DiscreteEventSimulation:
    """One run of the per-request model over a Stage 3A workload and a check schedule."""

    def __init__(
        self,
        workload: Workload,
        config: DesConfig | None = None,
        check_schedule: Sequence[CheckRelease] = (),
        controller: Controller | None = None,
        decision_interval_s: float = 1.0,
    ) -> None:
        if not isinstance(workload, Workload):
            raise TypeError("workload must be a Stage 3A Workload")
        self._config = config or DesConfig()
        if not isinstance(self._config, DesConfig):
            raise TypeError("config must be a DesConfig")
        scenario = workload.manifest.config
        self._workload = workload
        self._policy = scenario.timing_policy
        self._horizon = float(self._config.horizon_s or scenario.duration_s)
        self._routes = tuple(route.route_id for route in scenario.routes)
        self._check_latency = {
            r.route_id: r.beneficiary_median_latency_ms / 1000.0 for r in scenario.routes
        }

        ids = [p.transaction_id for p in workload.exogenous.payments]
        if len(ids) != len(set(ids)):
            raise DesError("workload contains duplicate payment transaction_ids")
        self._truth = {t.transaction_id: t for t in workload.ground_truth.beneficiary}
        self._behaviour = {b.transaction_id: b for b in workload.exogenous.customer_behaviour}
        if set(self._truth) != set(ids) or set(self._behaviour) != set(ids):
            raise DesError("every payment needs exactly one truth and one behaviour record")
        check_ids = [c.check_id for c in workload.exogenous.check_demands]
        if len(check_ids) != len(set(check_ids)):
            raise DesError("workload contains duplicate background check_ids")
        if set(check_ids) & set(ids):
            raise DesError("a background check_id collides with a transaction_id")

        route_changes: dict[str, list[tuple[float, float]]] = {r: [] for r in self._routes}
        for change in workload.ground_truth.capacity:
            value = change.capacity_rps
            if not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
                raise DesError(f"invalid capacity {value!r} on route {change.route_id}")
            if change.route_id not in route_changes:
                raise DesError(f"capacity change names unknown route {change.route_id}")
            route_changes[change.route_id].append((change.offset_s, float(value)))

        grouped = {r: g for g in self._config.bottleneck_groups for r in g}
        if unknown := sorted(set(grouped) - set(self._routes)):
            raise DesError(f"bottleneck_groups name unknown routes: {unknown}")
        for release in check_schedule:
            if release.route_id not in self._routes:
                raise DesError(f"check schedule names unknown route {release.route_id}")
        for demand in workload.exogenous.check_demands:
            if demand.route_id not in self._routes:
                raise DesError(f"check demand names unknown route {demand.route_id}")

        self._env = simpy.Environment()
        self._bottlenecks: dict[str, _Bottleneck] = {}
        self._bottleneck_of: dict[str, _Bottleneck] = {}
        for route in self._routes:
            members = grouped.get(route, (route,))
            name = "+".join(members)
            if name not in self._bottlenecks:
                points = sorted({0.0} | {o for m in members for o, _ in route_changes[m]})
                caps = [sum(self._route_capacity(route_changes[m], p) for m in members)
                        for p in points]  # fmt: skip
                seed = hashlib.sha256(
                    f"aorts-des|{self._config.service_seed}|{name}".encode()
                ).digest()
                self._bottlenecks[name] = _Bottleneck(
                    name,
                    members,
                    points,
                    caps,
                    random.Random(int.from_bytes(seed[:8], "big")),  # noqa: S311 - simulation
                )
            self._bottleneck_of[route] = self._bottlenecks[name]

        self._schedule = sorted(check_schedule, key=lambda r: (r.time_s, r.route_id))
        self._requests: list[_Request] = []
        self._payments: dict[str, _Payment] = {}
        self._checks: list[_Check] = []
        self._released: list[tuple[float, str]] = []
        self._ambiguous: dict[str, dict[str, _Payment]] = {r: {} for r in self._routes}
        self._ran = False
        if not math.isfinite(decision_interval_s) or decision_interval_s <= 0:
            raise DesError("decision_interval_s must be positive and finite")
        self._controller = controller
        self._decision_interval = float(decision_interval_s)
        # Causal observation logs, kept only for closed-loop runs.
        self._log_ambiguous: deque = deque()
        self._log_resolutions: list = []
        self._log_answers: list = []
        self._log_timeouts: deque = deque()
        self._outstanding: dict[str, set[str]] = {r: set() for r in self._routes}
        self._seq = 0

    @staticmethod
    def _route_capacity(changes: list[tuple[float, float]], t: float) -> float:
        current = 0.0
        for offset, value in sorted(changes):
            if offset <= t:
                current = value
        return current

    # ------------------------------------------------------------------- execution

    def run(self) -> DesResult:
        if self._ran:
            raise DesError("a DiscreteEventSimulation runs once")
        self._ran = True
        env = self._env
        for bottleneck in self._bottlenecks.values():
            bottleneck.wakeup = env.event()
            env.process(self._server(bottleneck))
        env.process(self._arrivals())
        env.process(self._releases())
        if self._controller is not None:
            env.process(self._control_loop())
        env.run(until=self._horizon)
        for bottleneck in self._bottlenecks.values():
            self._purge(bottleneck, self._horizon)  # stale drops due before the horizon
        return self._result()

    def _arrivals(self):
        env = self._env
        items = [
            (p.offset_s, _CLASS_ORDER[DesClass.PAYMENT], p.transaction_id, "P", p)
            for p in self._workload.exogenous.payments
        ] + [
            (c.offset_s, _CLASS_ORDER[DesClass.BACKGROUND_CHECK], c.check_id, "C", c)
            for c in self._workload.exogenous.check_demands
        ]
        items.sort(key=lambda item: item[:3])
        for offset, _, ident, tag, record in items:
            if offset >= self._horizon:
                break
            if offset > env.now:
                yield env.timeout(offset - env.now)
            if tag == "C":
                self._enqueue(_Request(DesClass.BACKGROUND_CHECK, ident, record.route_id, env.now))
                continue
            truth, behaviour = self._truth[ident], self._behaviour[ident]
            payment = _Payment(
                txn=ident,
                route=record.route_id,
                kind=DesClass.PAYMENT,
                sent_s=env.now,
                deadline_s=env.now + self._policy.client_timeout_s,
                credited=truth.credited,
                latency_s=truth.beneficiary_latency_ms / 1000.0,
                lost=truth.response_lost,
                confirm_s=truth.confirmation_delay_s,
                would_repay=behaviour.would_repay,
                repay_delay_s=behaviour.repay_delay_s,
                would_resubmit=behaviour.would_resubmit_same_key,
                resubmit_delay_s=behaviour.resubmit_delay_s,
            )
            self._start_payment(payment)

    def _start_payment(self, payment: _Payment) -> None:
        self._payments[payment.txn] = payment
        request = _Request(payment.kind, payment.txn, payment.route, self._env.now, payment=payment)
        payment.request = request
        self._enqueue(request)
        self._at(payment.deadline_s, lambda: self._on_deadline(payment))

    def _releases(self):
        env = self._env
        for release in self._schedule:
            if release.time_s >= self._horizon:
                break
            if release.time_s > env.now:
                yield env.timeout(release.time_s - env.now)
            self._release(release)

    def _release(self, release: CheckRelease) -> None:
        now = self._env.now
        candidates = sorted(
            self._ambiguous[release.route_id].values(), key=lambda p: (p.ambiguous_s, p.txn)
        )
        chosen = []
        for payment in candidates:
            if len(chosen) >= release.count:
                break
            if self._eligible(payment, now):
                chosen.append(payment)
        for payment in chosen:
            self._release_payment(payment, now)

    def _release_payment(self, payment: _Payment, now: float) -> "_Check":
        payment.checks_counted += 1
        payment.last_check_s = now
        check = _Check(
            f"CK-{payment.txn}-{payment.checks_counted}",
            payment,
            now,
            now + self._policy.check_timeout_s,
        )
        payment.check_in_flight = check
        self._checks.append(check)
        self._released.append((now, payment.route))
        check.request = _Request(
            DesClass.STATUS_CHECK,
            check.check_id,
            payment.route,
            now,
            payment=payment,
            check=check,
        )
        self._enqueue(check.request)
        self._at(check.deadline_s, lambda c=check: self._on_check_deadline(c))
        return check

    # ------------------------------------------------------------- closed-loop control

    def _control_loop(self):
        env = self._env
        previous, feedback, k = 0.0, None, 0
        while env.now < self._horizon:
            now = env.now
            observation = self._causal_observation(k, previous, now)
            requests = tuple(self._controller(now, observation, feedback))
            feedback = self._apply_requests(requests, now)
            previous, k = now, k + 1
            yield env.timeout(self._decision_interval)

    def _apply_requests(self, requests: tuple, now: float) -> ReleaseFeedback:
        released, refused, seen = [], [], set()
        for request in requests:
            if not isinstance(request, CheckRequest):
                raise TypeError("controllers must return CheckRequest values")
            if request.route_id not in self._ambiguous:
                raise DesError(f"controller names unknown route {request.route_id}")
            for txn in request.transaction_ids:
                payment = self._ambiguous[request.route_id].get(txn)
                if txn in seen:
                    refused.append((txn, "duplicate_request"))
                elif payment is None or payment.ambiguous_s is None or payment.ambiguous_s >= now:
                    refused.append((txn, "not_observed_ambiguous"))
                elif payment.resolved_by(now):
                    refused.append((txn, "resolved"))
                elif payment.check_in_flight is not None:
                    refused.append((txn, "in_flight"))
                elif not self._eligible(payment, now):
                    refused.append((txn, "timing_policy"))
                else:
                    released.append((txn, self._release_payment(payment, now).check_id))
                seen.add(txn)
        return ReleaseFeedback(now, tuple(released), tuple(refused))

    def _log(self, heap: list, when: float, item) -> None:
        self._seq += 1
        heapq.heappush(heap, (when, self._seq, item))

    def _causal_observation(self, k: int, start: float, end: float) -> "DesObservation":
        """Everything revealed in ``[start, end)``; nothing at or after ``end``."""

        ambiguous, resolutions, answers, timeouts = [], [], [], []
        while self._log_ambiguous and self._log_ambiguous[0][0] < end:
            _, payment = self._log_ambiguous.popleft()
            ambiguous.append(payment.txn)
            self._outstanding[payment.route].add(payment.txn)
        while self._log_resolutions and self._log_resolutions[0][0] < end:
            when, _, (payment, how) = heapq.heappop(self._log_resolutions)
            if payment.resolved_s != when or payment.resolved_how != how:
                continue  # superseded by an earlier resolution
            resolutions.append((payment.txn, _observed(payment), how, when))
            self._outstanding[payment.route].discard(payment.txn)
        while self._log_answers and self._log_answers[0][0] < end:
            when, _, check = heapq.heappop(self._log_answers)
            answers.append((check.check_id, check.payment.txn, check.result.value, when))
        while self._log_timeouts and self._log_timeouts[0][0] < end:
            timeouts.append(self._log_timeouts.popleft()[1].check_id)
        return DesObservation(
            k,
            start,
            end,
            tuple(sorted(ambiguous)),
            tuple(sorted(resolutions, key=lambda x: (x[3], x[0]))),
            tuple(sorted(answers, key=lambda x: (x[3], x[0]))),
            tuple(sorted(timeouts)),
            tuple((r, tuple(sorted(self._outstanding[r]))) for r in self._routes),
        )

    def _eligible(self, payment: _Payment, now: float) -> bool:
        if payment.resolved_by(now) or payment.check_in_flight is not None:
            return False
        if not self._config.enforce_timing_policy:
            return True
        policy = self._policy
        if payment.checks_counted >= policy.max_counted_checks:
            return False
        reference = (
            payment.sent_s
            if policy.first_check_reference is FirstCheckReference.INITIATED
            else payment.ambiguous_s
        )
        if now < reference + policy.first_check_delay_s or now > reference + policy.check_window_s:
            return False
        return payment.last_check_s is None or now >= payment.last_check_s + (
            policy.min_check_spacing_s
        )

    # ---------------------------------------------------------------------- queue

    def _enqueue(self, request: _Request) -> None:
        bottleneck = self._bottleneck_of[request.route]
        if self._config.service is ServiceDistribution.EXPONENTIAL:
            request.work = bottleneck.rng.expovariate(1.0)
        self._requests.append(request)
        bottleneck.waiting.append(request)
        if not bottleneck.wakeup.triggered:
            bottleneck.wakeup.succeed()

    def _purge(self, bottleneck: _Bottleneck, now: float) -> None:
        age = self._config.stale_drop_age_s
        if age is None:
            return
        while bottleneck.waiting and now - bottleneck.waiting[0].enqueued_s >= age:
            request = bottleneck.waiting.popleft()
            request.dropped_s = request.enqueued_s + age
            check = request.check
            if check is not None and check.answer_s is None and not check.timed_out:
                check.timed_out = True
                self._clear_in_flight(check)

    def _server(self, bottleneck: _Bottleneck):
        env = self._env
        while True:
            self._purge(bottleneck, env.now)
            if not bottleneck.waiting:
                bottleneck.wakeup = env.event()
                yield bottleneck.wakeup
                continue
            if bottleneck.capacity_at(env.now) <= 0:
                # A stopped server takes nothing into service: waiting requests stay subject to
                # the stale-drop rule until capacity returns.
                resume = bottleneck.next_positive(env.now)
                until = self._horizon if resume is None else min(resume, self._horizon)
                yield env.timeout(until - env.now + (1.0 if resume is None else 0.0))
                continue
            request = bottleneck.waiting.popleft()
            request.started_s = env.now
            finish = bottleneck.finish_time(env.now, request.work)
            request.planned_finish_s = finish
            bottleneck.in_service = request
            if finish == _INF:
                yield env.timeout(self._horizon - env.now + 1.0)  # never completes in the run
                continue
            yield env.timeout(finish - env.now)
            bottleneck.in_service = None
            request.finished_s = env.now
            self._complete(request)

    def _complete(self, request: _Request) -> None:
        if request.completed:
            raise DesError(f"duplicate completion of {request.ident}")
        request.completed = True
        now = self._env.now
        payment = request.payment
        if request.cls in (DesClass.PAYMENT, DesClass.REPAYMENT):
            payment.forwarded_s = payment.forwarded_s if payment.forwarded_s is not None else now
            if payment.processed_s is None:
                payment.processed_s = now + payment.latency_s
            if not payment.lost:
                payment.response_s = now + payment.latency_s + payment.confirm_s
                self._at(payment.response_s, lambda: self._on_response(payment))
        elif request.cls is DesClass.RESUBMISSION:
            if payment.processed_s is None:  # idempotent same-key processing
                payment.forwarded_s = now
                payment.processed_s = now + payment.latency_s
            if not payment.lost:
                answer = now + payment.latency_s + payment.confirm_s
                self._schedule_resolution(payment, answer, "RESUBMISSION")
                self._at(answer, lambda: self._settle_if_resolved(payment))
        elif request.cls is DesClass.STATUS_CHECK:
            check = request.check
            if check.timed_out:
                return  # served after its timeout: capacity used, nothing learned
            self._purge(self._bottleneck_of[payment.route], now)
            if payment.processed_s is not None and payment.processed_s <= now:
                result = (
                    CheckResultKind.AUTHORITATIVE_SUCCESS
                    if payment.credited
                    else CheckResultKind.AUTHORITATIVE_FAILURE
                )
            elif payment.dropped_unforwarded_by(now):
                result = CheckResultKind.UNKNOWN
            else:
                result = CheckResultKind.PENDING
            answer = now + self._check_latency[payment.route]
            if answer <= check.deadline_s:
                check.answer_s, check.result = answer, result
                self._at(answer, lambda: self._on_check_answer(check))
                if self._controller is not None:
                    self._log(self._log_answers, answer, check)
                authoritative = result in (
                    CheckResultKind.AUTHORITATIVE_SUCCESS,
                    CheckResultKind.AUTHORITATIVE_FAILURE,
                )
                if authoritative:
                    self._schedule_resolution(payment, answer, "CHECK")

    # --------------------------------------------------------------- remitter side

    def _at(self, when: float, action) -> None:
        if when >= self._horizon:
            return
        event = self._env.timeout(max(0.0, when - self._env.now))
        event.callbacks.append(lambda _event: action())

    def _schedule_resolution(self, payment: _Payment, when: float, how: str) -> None:
        if payment.resolved_s is None or when < payment.resolved_s:
            payment.resolved_s, payment.resolved_how = when, how
            if self._controller is not None:
                self._log(self._log_resolutions, when, (payment, how))

    def _on_deadline(self, payment: _Payment) -> None:
        if self._timely(payment):
            return  # decided by the known response instant, not by event order
        payment.ambiguous_s = payment.deadline_s
        if not payment.resolved_by(payment.deadline_s):
            self._ambiguous[payment.route][payment.txn] = payment
            if self._controller is not None:
                self._log_ambiguous.append((payment.deadline_s, payment))
        if payment.kind is DesClass.PAYMENT and not payment.customer_acted:
            payment.customer_acted = True
            if payment.would_repay:
                due = payment.deadline_s + payment.repay_delay_s
                self._at(due, lambda: self._on_repay(payment, due))
            if payment.would_resubmit:
                due = payment.deadline_s + payment.resubmit_delay_s
                self._at(due, lambda: self._on_resubmit(payment, due))

    def _timely(self, payment: _Payment) -> bool:
        if payment.response_s is not None:
            return payment.response_s <= payment.deadline_s
        finish = payment.request.planned_finish_s if payment.request else None
        if finish is None or payment.lost or payment.forwarded_s is not None:
            return False
        return finish + payment.latency_s + payment.confirm_s <= payment.deadline_s

    def _on_response(self, payment: _Payment) -> None:
        if payment.response_s > payment.deadline_s:
            self._schedule_resolution(payment, payment.response_s, "LATE_RESPONSE")
        self._settle_if_resolved(payment)

    def _on_check_answer(self, check: _Check) -> None:
        self._clear_in_flight(check)
        self._settle_if_resolved(check.payment)

    def _on_check_deadline(self, check: _Check) -> None:
        if check.answer_s is not None and check.answer_s <= check.deadline_s:
            return
        finish = check.request.planned_finish_s
        latency = self._check_latency[check.payment.route]
        if not check.timed_out and finish is not None and finish + latency <= check.deadline_s:
            return  # completes exactly now; its answer arrives in time
        check.timed_out = True
        self._clear_in_flight(check)
        if self._controller is not None:
            self._log_timeouts.append((check.deadline_s, check))

    def _clear_in_flight(self, check: _Check) -> None:
        if check.payment.check_in_flight is check:
            check.payment.check_in_flight = None

    def _settle_if_resolved(self, payment: _Payment) -> None:
        if payment.resolved_by(self._env.now):
            self._ambiguous[payment.route].pop(payment.txn, None)

    def _on_repay(self, payment: _Payment, due: float) -> None:
        if payment.resolved_by(due):
            return
        self._start_payment(
            _Payment(
                txn=f"RP-{payment.txn}",
                route=payment.route,
                kind=DesClass.REPAYMENT,
                sent_s=due,
                deadline_s=due + self._policy.client_timeout_s,
                credited=payment.credited,
                latency_s=payment.latency_s,
                lost=payment.lost,
                confirm_s=payment.confirm_s,
            )
        )

    def _on_resubmit(self, payment: _Payment, due: float) -> None:
        if payment.resolved_by(due):
            return
        self._enqueue(_Request(DesClass.RESUBMISSION, f"RS-{payment.txn}", payment.route, due,
                               payment=payment))  # fmt: skip

    # --------------------------------------------------------------------- results

    def _result(self) -> DesResult:
        horizon, bin_s = self._horizon, self._config.bin_s
        bins = max(1, math.ceil(horizon / bin_s - 1e-9))

        def index(t: float | None) -> int | None:
            if t is None or t >= horizon:
                return None
            return min(bins - 1, int(math.floor(t / bin_s + 1e-12)))

        names = (
            "payment_arrivals", "repayment_arrivals", "resubmission_arrivals", "checks_released",
            "background_check_arrivals", "served", "dropped", "new_ambiguous", "timely_successes",
            "timely_failures", "late_responses", "resolved", "check_results", "wasted_checks",
        )  # fmt: skip
        counts = {r: {n: [0] * bins for n in names} for r in self._routes}

        def bump(route: str, name: str, t: float | None) -> None:
            k = index(t)
            if k is not None:
                counts[route][name][k] += 1

        arrival_name = {
            DesClass.PAYMENT: "payment_arrivals",
            DesClass.REPAYMENT: "repayment_arrivals",
            DesClass.RESUBMISSION: "resubmission_arrivals",
            DesClass.BACKGROUND_CHECK: "background_check_arrivals",
        }
        depth_delta = {r: [0] * (bins + 1) for r in self._routes}
        for request in self._requests:
            if request.cls in arrival_name:
                bump(request.route, arrival_name[request.cls], request.enqueued_s)
            bump(request.route, "served", request.finished_s)
            bump(request.route, "dropped", request.dropped_s)
            # queue depth at t = enqueued before t and not ended before t (t = bin end)
            # sampled at t_j = j * bin_s (end of bin j - 1): counted iff enq < t_j <= end
            first = math.floor(request.enqueued_s / bin_s + 1e-12) + 1
            end = request.finished_s if request.finished_s is not None else request.dropped_s
            stop = bins + 1 if end is None else math.floor(end / bin_s + 1e-12) + 1
            if first <= bins and stop > first:
                depth_delta[request.route][first] += 1
                if stop <= bins:
                    depth_delta[request.route][stop] -= 1
        for when, route in self._released:
            bump(route, "checks_released", when)

        observations_by_bin: dict[int, dict] = {}

        def obs(k: int | None) -> dict | None:
            if k is None:
                return None
            return observations_by_bin.setdefault(
                k, {"ambiguous": [], "resolutions": [], "checks": [], "timeouts": []}
            )

        traces = []
        for payment in self._payments.values():
            timely = payment.response_s is not None and payment.response_s <= payment.deadline_s
            if timely:
                name = "timely_successes" if payment.credited else "timely_failures"
                bump(payment.route, name, payment.response_s)
                if (o := obs(index(payment.response_s))) is not None:
                    o["resolutions"].append(
                        (payment.txn, _observed(payment), "TIMELY_RESPONSE", payment.response_s)
                    )
            elif payment.response_s is not None:
                bump(payment.route, "late_responses", payment.response_s)
            if payment.ambiguous_s is not None:
                bump(payment.route, "new_ambiguous", payment.ambiguous_s)
                if (o := obs(index(payment.ambiguous_s))) is not None:
                    o["ambiguous"].append(payment.txn)
                if payment.resolved_s is not None:
                    bump(payment.route, "resolved", payment.resolved_s)
                    if (o := obs(index(payment.resolved_s))) is not None:
                        o["resolutions"].append(
                            (payment.txn, _observed(payment), payment.resolved_how,
                             payment.resolved_s)
                        )  # fmt: skip
            traces.append(
                TransactionTrace(
                    payment.txn,
                    payment.route,
                    payment.kind.value,
                    payment.sent_s,
                    payment.response_s if payment.response_s is not None
                    and payment.response_s < horizon else None,
                    payment.ambiguous_s,
                    payment.resolved_s if payment.resolved_s is not None
                    and payment.resolved_s < horizon else None,
                    payment.resolved_how if payment.resolved_s is not None
                    and payment.resolved_s < horizon else None,
                )
            )  # fmt: skip
        for check in self._checks:
            route = check.payment.route
            if check.answer_s is not None and check.answer_s <= check.deadline_s:
                bump(route, "check_results", check.answer_s)
                if (o := obs(index(check.answer_s))) is not None:
                    o["checks"].append(
                        (check.check_id, check.payment.txn, check.result.value, check.answer_s)
                    )
            else:
                wasted_at = check.deadline_s  # a stale drop (30 s) always comes after this
                bump(route, "wasted_checks", wasted_at)
                if (o := obs(index(wasted_at))) is not None:
                    o["timeouts"].append(check.check_id)

        depth = {}
        for route in self._routes:
            running, series = 0, []
            for k in range(1, bins + 1):
                running += depth_delta[route][k]
                series.append(running)
            depth[route] = series

        # Outstanding ambiguity at each bin end: ambiguous before the end, not resolved before it.
        changes = []
        for p in self._payments.values():
            if p.ambiguous_s is not None:
                changes.append((p.ambiguous_s, 1, p.route, p.txn))
                if p.resolved_s is not None:
                    changes.append((p.resolved_s, -1, p.route, p.txn))
        sweep = deque(sorted(changes, key=lambda c: (c[0], -c[1], c[3])))
        open_ambiguous: dict[str, set[str]] = {r: set() for r in self._routes}
        intervals, observations = [], []
        for k in range(bins):
            start = k * bin_s
            for route in self._routes:
                group = self._bottleneck_of[route].routes
                intervals.append(
                    DesInterval(
                        bin_index=k,
                        start_s=start,
                        route_id=route,
                        **{n: counts[route][n][k] for n in names},
                        queue_depth=depth[route][k],
                        bottleneck_queue_depth=sum(depth[r][k] for r in group),
                    )
                )
            o = observations_by_bin.get(k, {"ambiguous": [], "resolutions": [], "checks": [],
                                             "timeouts": []})  # fmt: skip
            end = (k + 1) * bin_s
            while sweep and sweep[0][0] < end:
                _, sign, route, txn = sweep.popleft()
                if sign > 0:
                    open_ambiguous[route].add(txn)
                else:
                    open_ambiguous[route].discard(txn)
            outstanding = tuple(
                (route, tuple(sorted(open_ambiguous[route]))) for route in self._routes
            )
            observations.append(
                DesObservation(
                    k,
                    start,
                    end,
                    tuple(sorted(o["ambiguous"])),
                    tuple(sorted(o["resolutions"], key=lambda x: (x[3], x[0]))),
                    tuple(sorted(o["checks"], key=lambda x: (x[3], x[0]))),
                    tuple(sorted(o["timeouts"])),
                    outstanding,
                )
            )

        totals = Counter()
        for interval in intervals:
            for name in names:
                totals[name] += getattr(interval, name)
        for cls in DesClass:
            members = [r for r in self._requests if r.cls is cls and r.enqueued_s < horizon]
            totals[f"arrived_{cls.value}"] = len(members)
            totals[f"completed_{cls.value}"] = sum(
                1 for r in members if r.finished_s is not None and r.finished_s < horizon
            )
            totals[f"dropped_{cls.value}"] = sum(
                1 for r in members if r.dropped_s is not None and r.dropped_s < horizon
            )
            totals[f"queued_{cls.value}"] = sum(1 for r in members if not r.ended_by(horizon))
        totals["queued_at_end"] = sum(1 for r in self._requests if not r.ended_by(horizon))
        totals["unresolved_at_end"] = sum(len(o) for _, o in observations[-1].ambiguous_outstanding)
        for how in ("LATE_RESPONSE", "CHECK", "RESUBMISSION"):
            totals[f"resolved_by_{how.lower()}"] = sum(
                1
                for p in self._payments.values()
                if p.ambiguous_s is not None
                and p.resolved_how == how
                and p.resolved_s is not None
                and p.resolved_s < horizon
            )
        totals_tuple = tuple(sorted(totals.items()))
        manifest = self._workload.manifest
        digest = hashlib.sha256()
        digest.update(manifest.content_hash.encode())
        digest.update(self._config.model_dump_json().encode())
        for interval in intervals:
            digest.update(json.dumps(asdict(interval), sort_keys=True).encode())
        for trace in sorted(traces, key=lambda t: t.txn):
            digest.update(json.dumps(asdict(trace), sort_keys=True).encode())
        return DesResult(
            model_version=DES_MODEL_VERSION,
            experiment_id=manifest.experiment_id,
            run_id=manifest.run_id,
            scenario_id=manifest.scenario_id,
            workload_content_hash=manifest.content_hash,
            config=self._config,
            horizon_s=horizon,
            bins=bins,
            intervals=tuple(intervals),
            observations=tuple(observations),
            traces=tuple(sorted(traces, key=lambda t: (t.sent_s, t.txn))),
            totals=totals_tuple,
            digest=digest.hexdigest(),
        )


def _observed(payment: _Payment) -> str:
    return "SUCCESS" if payment.credited else "FAILURE"


def simulate_des(
    workload: Workload,
    config: DesConfig | None = None,
    check_schedule: Iterable[CheckRelease] = (),
    controller: Controller | None = None,
    decision_interval_s: float = 1.0,
) -> DesResult:
    return DiscreteEventSimulation(
        workload, config, tuple(check_schedule), controller, decision_interval_s
    ).run()


def conservation_residuals(result: DesResult) -> dict[str, int]:
    """arrived - completed - dropped - still queued, per request class; all must be zero."""

    residuals = {
        cls.value: result.total(f"arrived_{cls.value}")
        - result.total(f"completed_{cls.value}")
        - result.total(f"dropped_{cls.value}")
        - result.total(f"queued_{cls.value}")
        for cls in DesClass
    }
    residuals["queued_at_end"] = result.total("queued_at_end") - sum(
        result.total(f"queued_{cls.value}") for cls in DesClass
    )
    return residuals
