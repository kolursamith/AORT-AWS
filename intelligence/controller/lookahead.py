"""Feedback-aware counterfactual look-ahead for recovery-check budgets (Stage 07).

``forecast_budgets`` predicts, for each candidate check budget and under one shared forecast of
exogenous payment load, what the next ``horizon_s`` seconds would look like on one route:
queueing, payment delay, payment timeouts (new ambiguity), check completions and their
expected authoritative answers, re-payment pressure and the ambiguous backlog. The zero-check
counterfactual is always evaluated, so every positive budget is reported as an increment over
it. This is a forecast model, not the simulator: it never sees true capacity, future faults,
beneficiary decisions or any hidden queue. Its inputs are the Stage 06 ``CapacityEstimate``,
observable route and transaction facts and the timing policy.

Model (fixed step ``step_s``, ``n = ceil(horizon_s / step_s)`` steps, capped at ``max_steps``)
--------------------------------------------------------------------------------------------
Units: rates in requests/s, counts in expected requests (transactions), times in s.

* Capacity ``c`` available to own traffic, per band. Let ``L'`` = the estimator's lower bound
  ``L``, raised to the smoothed own offered load when the route keeps up with its load: the
  estimator reports it HEALTHY with no current signal, or answered requests show no queueing
  (observed median latency below ``latency_floor_s + healthy_wait_s``). Lost confirmations
  produce timeouts that the estimator cannot tell from congestion; the latency of the answers
  that do arrive can. IDENTIFIED: PESSIMISTIC = ``min(L', point)``, CENTRAL = point, OPTIMISTIC =
  ``1.05 x`` point. Bound only: PESSIMISTIC = ``L'``, OPTIMISTIC =
  ``optimistic_capacity_factor x L'``, CENTRAL = their midpoint (a declared assumption:
  nothing above ``L'`` is measured). Capacity is held constant over the horizon (no fault is
  predicted).
* Exogenous payment arrivals ``lambda`` = the estimator's smoothed own offered load minus the
  current own check rate (checks are counted separately, never twice). The same ``lambda`` is
  used for every candidate budget.
* Initial queue: with an observed median latency ``m`` of answered requests, waiting time
  ``w0 = min(stale_drop_age_s, max(0, m - latency_floor_s))`` and ``Q0 = lambda_offered x w0``
  (Little's law on the flow entering the queue), spread evenly over ages ``0..w0``; without
  it, ``Q0 = max(0, q_obs - lambda x latency_floor_s)`` from own outstanding requests, which
  overcounts when confirmations are lost and undercounts beyond the client timeout.
* Check releases: a credit of ``b x step_s`` per step (unused credit is not banked beyond one
  check) releases whole checks to candidates already eligible (``eligible_at <= t``), each at
  most once in the horizon, in the order given (the optimizer passes them ranked).
* Queue: FIFO cohorts by arrival step (at most ``stale_drop_age_s / step_s`` of them).
  ``A_k = lambda dt + repay_k + checks_k`` joins; ``c dt`` of work is served from the oldest
  cohort; work older than ``stale_drop_age_s`` is dropped (by age, as the simulators do, so an
  overloaded queue holds about ``arrival rate x stale_drop_age_s``). Conservation: ``Q0 + sum A
  = sum S + sum D + Q_end`` (reported as ``conservation_residual``).
* Wait seen by a request arriving in step ``k``: ``W_k = min(Q_k / c, stale_drop_age_s)``
  (FIFO; ``stale_drop_age_s`` when ``c = 0``). Payment delay = ``W_k + latency_floor_s``.
* Timeout probability ``f(W) = 1 - (1 - f0)(1 - sigmoid((W + latency_floor_s - T_client) /
  timeout_softness_s))`` where ``f0`` is the non-queueing timeout fraction (lost
  confirmations): the observed recent timeout rate when answered latency shows no queue, else
  0 (unidentifiable during congestion); new ambiguity ``N_k = (lambda dt + repay_k)
  f(W_k)``; a check is answered in time with probability
  ``1 - sigmoid((W + latency_floor_s - T_check) / timeout_softness_s)``.
* Feedback chain (the mechanism under study): extra checks -> larger ``Q`` -> larger ``W`` ->
  more timeouts -> extra ambiguous payments -> (a) bounded re-payment demand
  ``repay_{k + d} += repay_probability x N_k`` injected ``repay_delay_s`` later, one generation
  only (re-payments never trigger re-payments), and (b) induced future check demand ``N x
  max_counted_checks``, reported but not injected (it arrives after the policy's first-check
  delay and depends on the future policy).
* Backlog: ``B <- max(0, B + N_k - h B dt - R_k)`` with ``h`` the observed late-resolution
  hazard and ``R_k`` the expected authoritative check answers of step ``k``.
* Check benefit: the probability that a check answers authoritatively is estimated from this
  route's observed check outcomes (Beta(1, 4) prior; a one-sided 90 % lower bound), times
  declared class factors: last check UNKNOWN (the switch had no record, so the payment was
  probably never forwarded) x ``unknown_factor``; sent while the route was congested x
  ``congested_factor``. With fewer than ``min_check_observations`` outcomes the benefit is
  unidentified and the conservative ``prior_low`` is used.

Ablations (``FeedbackMode``): FULL; NO_FEEDBACK (check load is excluded from the queue seen by
payments, so checks cannot create payment timeouts; check completion still uses the real
queue); NO_REPAY (re-payment demand off). The optimizer's no-look-ahead reference skips this
module. No ablation touches eligibility, fail-closed handling or limits.

Limits: one route and one bottleneck, no background load beyond what the estimate absorbed,
constant capacity, a smooth approximation of the per-request timeout distribution, horizon
capped at ``max_steps``. Disagreement between bands is reported, never hidden.
"""

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from contracts.v1_1 import CheckResultKind, TimingPolicy
from contracts.v1_1.models import NonNegativeFloat, PositiveFloat
from intelligence.ml.capacity_estimator import (
    CapacityEstimate,
    EstimatorMode,
    Identifiability,
    Reason,
    RouteState,
)

FORECAST_VERSION = "aorts-lookahead/1"


class FeedbackMode(StrEnum):
    FULL = "FULL"
    NO_FEEDBACK = "NO_FEEDBACK"
    NO_REPAY = "NO_REPAY"


class Band(StrEnum):
    PESSIMISTIC = "PESSIMISTIC"
    CENTRAL = "CENTRAL"
    OPTIMISTIC = "OPTIMISTIC"


class CheckEvidence(StrEnum):
    """What the remitter has observed about one ambiguous transaction (no hidden truth)."""

    NO_CHECK_YET = "NO_CHECK_YET"
    LAST_CHECK_UNKNOWN = "LAST_CHECK_UNKNOWN"  # the switch had no record at check time
    LAST_CHECK_PENDING = "LAST_CHECK_PENDING"  # inconclusive
    LAST_CHECK_TIMED_OUT = "LAST_CHECK_TIMED_OUT"  # no answer in time


class ForecastReason(StrEnum):
    CAPACITY_UNAVAILABLE = "CAPACITY_UNAVAILABLE"
    CAPACITY_STALE = "CAPACITY_STALE"
    ESTIMATOR_FAIL_CLOSED = "ESTIMATOR_FAIL_CLOSED"
    ESTIMATOR_INITIALIZING = "ESTIMATOR_INITIALIZING"
    CAPACITY_UNIDENTIFIED = "CAPACITY_UNIDENTIFIED"
    TRUST_DEGRADED = "TRUST_DEGRADED"
    FLAGGED_TELEMETRY = "FLAGGED_TELEMETRY"
    ROUTE_CONGESTED = "ROUTE_CONGESTED"
    QUEUE_UNOBSERVED = "QUEUE_UNOBSERVED"
    QUEUE_FROM_OUTSTANDING = "QUEUE_FROM_OUTSTANDING"
    NONQUEUE_TIMEOUTS = "NONQUEUE_TIMEOUTS"
    LATENCY_SHOWS_NO_QUEUE = "LATENCY_SHOWS_NO_QUEUE"
    CHECK_BENEFIT_UNIDENTIFIED = "CHECK_BENEFIT_UNIDENTIFIED"
    LATE_RESOLUTION_UNOBSERVED = "LATE_RESOLUTION_UNOBSERVED"
    NO_ELIGIBLE_CHECKS = "NO_ELIGIBLE_CHECKS"
    SATURATED = "SATURATED"
    BAND_DISAGREEMENT = "BAND_DISAGREEMENT"
    HORIZON_TRUNCATED = "HORIZON_TRUNCATED"


class ForecastInputError(ValueError):
    """Inputs that cannot describe one route at one instant (identity, sign or finiteness)."""


class LookaheadConfig(BaseModel):
    """Every forecast parameter; defaults are declared research assumptions."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    horizon_s: PositiveFloat = 60.0
    step_s: PositiveFloat = 1.0
    max_steps: int = Field(default=600, ge=1, le=10_000, strict=True)
    stale_drop_age_s: PositiveFloat = 30.0
    latency_floor_s: NonNegativeFloat = 0.3
    timeout_softness_s: PositiveFloat = 1.0
    healthy_wait_s: PositiveFloat = 1.0
    optimistic_capacity_factor: float = Field(default=1.25, ge=1.0, strict=True)
    repay_probability: float = Field(default=0.10, ge=0.0, le=1.0, strict=True)
    repay_delay_s: NonNegativeFloat = 30.0
    prior_low: float = Field(default=0.05, ge=0.0, le=1.0, strict=True)
    min_check_observations: int = Field(default=5, ge=1, strict=True)
    unknown_factor: float = Field(default=0.25, ge=0.0, le=1.0, strict=True)
    congested_factor: float = Field(default=0.5, ge=0.0, le=1.0, strict=True)
    feedback_mode: FeedbackMode = FeedbackMode.FULL

    def digest(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class CheckCandidate:
    """One ambiguous transaction that is, or will become, eligible for a status check."""

    experiment_id: str
    route_id: str
    transaction_id: str
    version: int
    eligible_at: datetime
    checks_used: int
    deadline_at: datetime | None
    evidence: CheckEvidence = CheckEvidence.NO_CHECK_YET
    sent_while_congested: bool | None = None
    remitter_bank_id: str | None = None


@dataclass(frozen=True, slots=True)
class CheckOutcomeCounts:
    """Recently observed answers to checks on this route (sensing data only)."""

    authoritative: int = 0
    unknown: int = 0
    pending: int = 0
    timed_out: int = 0

    @property
    def total(self) -> int:
        return self.authoritative + self.unknown + self.pending + self.timed_out

    def record(self, result: CheckResultKind | None) -> "CheckOutcomeCounts":
        """New counts with one more outcome (``None`` means the check got no answer in time)."""

        if result is None:
            return replace(self, timed_out=self.timed_out + 1)
        if result in (CheckResultKind.AUTHORITATIVE_SUCCESS, CheckResultKind.AUTHORITATIVE_FAILURE):
            return replace(self, authoritative=self.authoritative + 1)
        if result is CheckResultKind.UNKNOWN:
            return replace(self, unknown=self.unknown + 1)
        return replace(self, pending=self.pending + 1)


@dataclass(frozen=True)
class ForecastInput:
    """Observable state of one route at one decision instant."""

    experiment_id: str
    route_id: str
    decision_time: datetime
    capacity: CapacityEstimate
    observed_outstanding: int | None
    current_check_rps: float
    ambiguous_backlog: int
    late_resolution_hazard_per_s: float | None
    candidates: tuple[CheckCandidate, ...]
    check_outcomes: CheckOutcomeCounts = field(default_factory=CheckOutcomeCounts)
    observed_median_latency_s: float | None = None  # answered requests, recent windows
    observed_timeout_rate: float | None = None  # timeouts / determined, recent windows


@dataclass(frozen=True)
class BandForecast:
    """One budget under one capacity band. Counts are expectations over the horizon."""

    band: Band
    capacity_rps: float
    checks_released: float
    checks_on_time: float
    expected_authoritative: float
    payment_arrivals: float
    repay_arrivals: float
    served: float
    dropped: float
    queue_start: float
    queue_end: float
    queue_mean: float
    queue_max: float
    mean_payment_wait_s: float
    predicted_payment_delay_ms: float
    new_ambiguous: float
    backlog_end: float
    induced_check_demand: float
    deadline_misses: float
    conservation_residual: float


@dataclass(frozen=True)
class BudgetForecast:
    budget_rps: int
    bands: tuple[BandForecast, ...]
    incremental_ambiguity: float  # CENTRAL new ambiguity minus the zero-budget CENTRAL value
    incremental_ambiguity_pessimistic: float
    incremental_delay_ms: float

    def band(self, band: Band) -> BandForecast:
        return next(b for b in self.bands if b.band is band)


@dataclass(frozen=True)
class ForecastResult:
    """All candidate budgets for one route and instant, under the same assumptions."""

    forecast_version: str
    config_digest: str
    experiment_id: str
    route_id: str
    decision_time: datetime
    horizon_s: float
    steps: int
    available: bool
    feedback_mode: FeedbackMode
    exogenous_payment_rps: float
    eligible_now: int
    eligible_in_horizon: int
    check_benefit_low: float
    check_benefit_identified: bool
    budgets: tuple[BudgetForecast, ...]
    reasons: tuple[ForecastReason, ...]

    def budget(self, rate: int) -> BudgetForecast:
        return next(b for b in self.budgets if b.budget_rps == rate)

    def digest(self) -> str:
        data = asdict(self)
        return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


# --------------------------------------------------------------------- pure helpers


def sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def benefit_lower_bound(counts: CheckOutcomeCounts, config: LookaheadConfig) -> tuple[float, bool]:
    """Conservative probability that a check answers authoritatively, and whether identified."""

    n = counts.total
    if n < config.min_check_observations:
        return config.prior_low, False
    a, b = counts.authoritative + 1.0, (n - counts.authoritative) + 4.0
    mean = a / (a + b)
    sd = math.sqrt(a * b / ((a + b) ** 2 * (a + b + 1.0)))
    return max(0.0, mean - 1.2816 * sd), True  # one-sided 90 % normal approximation


def candidate_benefit(candidate: CheckCandidate, base: float, config: LookaheadConfig) -> float:
    p = base
    if candidate.evidence is CheckEvidence.LAST_CHECK_UNKNOWN:
        p *= config.unknown_factor
    if candidate.sent_while_congested:
        p *= config.congested_factor
    return min(1.0, max(0.0, p))


def _finite_nonneg(value, name: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ForecastInputError(f"{name} must be a number")
    if not math.isfinite(value) or value < 0:
        raise ForecastInputError(f"{name} must be finite and non-negative, got {value!r}")


def validate_input(data: ForecastInput) -> None:
    cap = data.capacity
    if not isinstance(cap, CapacityEstimate):
        raise ForecastInputError("capacity must be a Stage 06 CapacityEstimate")
    if (cap.experiment_id, cap.route_id) != (data.experiment_id, data.route_id):
        raise ForecastInputError("capacity estimate belongs to a different experiment or route")
    if data.decision_time.tzinfo is None or data.decision_time.utcoffset() is None:
        raise ForecastInputError("decision_time must be timezone-aware")
    if cap.decision_time > data.decision_time:
        raise ForecastInputError("capacity estimate is from the future")
    _finite_nonneg(data.observed_outstanding, "observed_outstanding")
    _finite_nonneg(data.current_check_rps, "current_check_rps")
    _finite_nonneg(data.ambiguous_backlog, "ambiguous_backlog")
    _finite_nonneg(data.late_resolution_hazard_per_s, "late_resolution_hazard_per_s")
    _finite_nonneg(data.observed_median_latency_s, "observed_median_latency_s")
    _finite_nonneg(data.observed_timeout_rate, "observed_timeout_rate")
    if data.observed_timeout_rate is not None and data.observed_timeout_rate > 1:
        raise ForecastInputError("observed_timeout_rate is a fraction in [0, 1]")
    for name in ("authoritative", "unknown", "pending", "timed_out"):
        _finite_nonneg(getattr(data.check_outcomes, name), f"check_outcomes.{name}")
    seen = set()
    for c in data.candidates:
        if (c.experiment_id, c.route_id) != (data.experiment_id, data.route_id):
            raise ForecastInputError(f"candidate {c.transaction_id} belongs to another route")
        if c.transaction_id in seen:
            raise ForecastInputError(f"candidate {c.transaction_id} is listed twice")
        seen.add(c.transaction_id)
        if c.version < 1 or c.checks_used < 0:
            raise ForecastInputError(f"candidate {c.transaction_id} has an invalid version")
        if c.eligible_at.tzinfo is None:
            raise ForecastInputError(f"candidate {c.transaction_id} eligible_at is naive")


# --------------------------------------------------------------------- the model


def latency_shows_no_queue(data: ForecastInput, config: LookaheadConfig) -> bool:
    m = data.observed_median_latency_s
    return m is not None and m < config.latency_floor_s + config.healthy_wait_s


def capacity_bands(
    cap: CapacityEstimate, config: LookaheadConfig, latency_ok: bool = False
) -> dict[Band, float]:
    low = cap.capacity_lower_bound_rps or 0.0
    keeping_up = latency_ok or (
        cap.route_state is RouteState.HEALTHY
        and Reason.CONGESTION_SIGNAL not in cap.reason_codes
        and Reason.SERVICE_DEFICIT not in cap.reason_codes
    )
    if keeping_up and cap.offered_load_rps is not None:
        low = max(low, cap.offered_load_rps)
    point = cap.estimated_capacity_rps
    if cap.identifiability is Identifiability.IDENTIFIED and point is not None:
        return {
            Band.PESSIMISTIC: min(low, point),
            Band.CENTRAL: point,
            Band.OPTIMISTIC: point * 1.05,
        }
    high = low * config.optimistic_capacity_factor
    return {Band.PESSIMISTIC: low, Band.CENTRAL: 0.5 * (low + high), Band.OPTIMISTIC: high}


@dataclass(frozen=True)
class _Plan:
    """Candidates ordered for release, with their benefit and first eligible step."""

    candidates: tuple[CheckCandidate, ...]
    benefits: tuple[float, ...]
    eligible_steps: tuple[int, ...]


def initial_queue(data: ForecastInput, config: LookaheadConfig, lam: float) -> tuple[float, float]:
    """(queued work, wait already accumulated by it in s) at the decision instant."""

    m = data.observed_median_latency_s
    if m is not None:
        wait = min(max(0.0, m - config.latency_floor_s), config.stale_drop_age_s)
        offered = data.capacity.offered_load_rps or 0.0
        return offered * wait, wait
    queue = max(0.0, (data.observed_outstanding or 0) - lam * config.latency_floor_s)
    return queue, 0.0


class _Cohorts:
    """FIFO queue of work grouped by arrival step; work older than the drop age leaves."""

    def __init__(self, max_age_steps: int) -> None:
        self.items: list[list[float]] = []  # [age_steps, amount], oldest first
        self.max_age = max_age_steps

    @property
    def total(self) -> float:
        return sum(amount for _, amount in self.items)

    def seed(self, amount: float, spread_steps: int) -> None:
        n = max(1, spread_steps)
        for age in range(n - 1, -1, -1):
            self.items.append([float(age), amount / n])

    def push(self, amount: float) -> None:
        if amount > 0:
            self.items.append([0.0, amount])

    def serve(self, work: float) -> float:
        served = 0.0
        while self.items and work > 1e-12:
            take = min(work, self.items[0][1])
            self.items[0][1] -= take
            work -= take
            served += take
            if self.items[0][1] <= 1e-12:
                self.items.pop(0)
        return served

    def age_and_drop(self) -> float:
        dropped = 0.0
        kept = []
        for item in self.items:
            item[0] += 1.0
            if item[0] > self.max_age:
                dropped += item[1]
            else:
                kept.append(item)
        self.items = kept
        return dropped


def simulate_band(
    data: ForecastInput,
    plan: _Plan,
    policy: TimingPolicy,
    config: LookaheadConfig,
    band: Band,
    capacity: float,
    budget: int,
    lam: float,
    steps: int,
    base_timeout: float = 0.0,
) -> BandForecast:
    """One deterministic forward pass of the aggregate model for one budget and band."""

    dt = config.step_s
    floor_s = config.latency_floor_s
    soft = config.timeout_softness_s
    feedback = config.feedback_mode is not FeedbackMode.NO_FEEDBACK
    repay_p = config.repay_probability if config.feedback_mode is not FeedbackMode.NO_REPAY else 0.0
    repay_lag = max(1, int(round(config.repay_delay_s / dt)))
    blocked = capacity <= 0
    max_age = max(1, int(round(config.stale_drop_age_s / dt)))

    q0, w0 = initial_queue(data, config, lam)
    queue = _Cohorts(max_age)
    queue.seed(q0, int(math.ceil(w0 / dt)))
    payments_only = _Cohorts(max_age)  # what payments wait behind in NO_FEEDBACK
    payments_only.seed(q0, int(math.ceil(w0 / dt)))
    start = queue.total
    backlog = float(data.ambiguous_backlog)
    hazard = data.late_resolution_hazard_per_s or 0.0
    repay = [0.0] * (steps + repay_lag + 1)
    credit = 0.0
    nxt = 0
    resolved_prob: dict[int, float] = {}
    totals = dict.fromkeys(
        (
            "released",
            "on_time",
            "auth",
            "arrivals",
            "served",
            "dropped",
            "amb",
            "repay",
            "payments",
            "wait_sum",
            "queue_sum",
        ),
        0.0,
    )
    queue_max = start

    def wait_of(q: float) -> float:
        return config.stale_drop_age_s if blocked else min(q / capacity, config.stale_drop_age_s)

    for k in range(steps):
        exo, rep = lam * dt, repay[k]
        credit = min(credit + budget * dt, max(1.0, budget * dt))
        released_now = []
        while credit >= 1.0 - 1e-9 and nxt < len(plan.candidates) and plan.eligible_steps[nxt] <= k:
            released_now.append(nxt)
            nxt += 1
            credit -= 1.0
        checks = float(len(released_now))

        w_all = wait_of(queue.total)
        w_pay = w_all if feedback else wait_of(payments_only.total)
        f_queue = sigmoid((w_pay + floor_s - policy.client_timeout_s) / soft)
        f_pay = 1.0 - (1.0 - base_timeout) * (1.0 - f_queue)
        f_chk = 1.0 - sigmoid((w_all + floor_s - policy.check_timeout_s) / soft)
        amb = (exo + rep) * f_pay
        answers = 0.0
        for i in released_now:
            resolved_prob[i] = f_chk * plan.benefits[i]
            answers += resolved_prob[i]
        if repay_p > 0:
            repay[k + repay_lag] += repay_p * amb

        arrivals = exo + rep + checks
        work = 0.0 if blocked else capacity * dt
        queue.push(arrivals)
        served = queue.serve(work)
        dropped = queue.age_and_drop()
        if not feedback:
            payments_only.push(exo + rep)
            payments_only.serve(work)
            payments_only.age_and_drop()
        q_now = queue.total

        totals["released"] += checks
        totals["on_time"] += checks * f_chk
        totals["auth"] += answers
        totals["arrivals"] += arrivals
        totals["served"] += served
        totals["dropped"] += dropped
        totals["amb"] += amb
        totals["repay"] += rep
        totals["payments"] += exo + rep
        totals["wait_sum"] += (exo + rep) * w_pay
        totals["queue_sum"] += q_now
        queue_max = max(queue_max, q_now)
        backlog = max(0.0, backlog + amb - hazard * backlog * dt - answers)
    queue_end = queue.total

    horizon_end = data.decision_time + timedelta(seconds=steps * dt)
    misses = 0.0
    for i, c in enumerate(plan.candidates):
        if c.deadline_at is not None and c.deadline_at <= horizon_end:
            misses += 1.0 - resolved_prob.get(i, 0.0)
    mean_wait = totals["wait_sum"] / totals["payments"] if totals["payments"] > 0 else 0.0
    return BandForecast(
        band=band,
        capacity_rps=capacity,
        checks_released=totals["released"],
        checks_on_time=totals["on_time"],
        expected_authoritative=totals["auth"],
        payment_arrivals=totals["payments"] - totals["repay"],
        repay_arrivals=totals["repay"],
        served=totals["served"],
        dropped=totals["dropped"],
        queue_start=start,
        queue_end=queue_end,
        queue_mean=totals["queue_sum"] / steps,
        queue_max=queue_max,
        mean_payment_wait_s=mean_wait,
        predicted_payment_delay_ms=(mean_wait + floor_s) * 1000.0,
        new_ambiguous=totals["amb"],
        backlog_end=backlog,
        induced_check_demand=totals["amb"] * policy.max_counted_checks,
        deadline_misses=misses,
        conservation_residual=start
        + totals["arrivals"]
        - totals["served"]
        - totals["dropped"]
        - queue_end,
    )


def _availability(cap: CapacityEstimate) -> ForecastReason | None:
    if cap.mode is EstimatorMode.FAIL_CLOSED:
        return ForecastReason.ESTIMATOR_FAIL_CLOSED
    if cap.mode is EstimatorMode.STALE:
        return ForecastReason.CAPACITY_STALE
    if cap.mode is EstimatorMode.INITIALIZING:
        return ForecastReason.ESTIMATOR_INITIALIZING
    if cap.mode is EstimatorMode.INSUFFICIENT_DATA:
        return ForecastReason.CAPACITY_UNAVAILABLE  # no served-work evidence to forecast from
    if cap.capacity_lower_bound_rps is None or cap.offered_load_rps is None:
        return ForecastReason.CAPACITY_UNAVAILABLE
    return None


def forecast_budgets(
    data: ForecastInput,
    budgets: Sequence[int],
    policy: TimingPolicy,
    config: LookaheadConfig | None = None,
) -> ForecastResult:
    """Forecast every budget (zero is always added) under one set of observable assumptions."""

    config = config or LookaheadConfig()
    if not isinstance(policy, TimingPolicy):
        raise ForecastInputError("policy must be a TimingPolicy")
    validate_input(data)
    if any(isinstance(b, bool) or not isinstance(b, int) or b < 0 for b in budgets):
        raise ForecastInputError("budgets must be non-negative integers (checks per second)")
    rates = tuple(sorted(set(budgets) | {0}))
    cap = data.capacity
    reasons: set[ForecastReason] = set()

    wanted = math.ceil(config.horizon_s / config.step_s - 1e-9)
    steps = min(wanted, config.max_steps)
    if steps < wanted:
        reasons.add(ForecastReason.HORIZON_TRUNCATED)
    unavailable = _availability(cap)
    if cap.mode is EstimatorMode.DEGRADED:
        reasons.add(ForecastReason.TRUST_DEGRADED)
    if Reason.FLAGGED_TELEMETRY_NEWER in cap.reason_codes:
        reasons.add(ForecastReason.FLAGGED_TELEMETRY)
    if cap.identifiability is not Identifiability.IDENTIFIED:
        reasons.add(ForecastReason.CAPACITY_UNIDENTIFIED)
    if cap.route_state is RouteState.CONGESTED:
        reasons.add(ForecastReason.ROUTE_CONGESTED)
    if data.observed_median_latency_s is None:
        if data.observed_outstanding is None:
            reasons.add(ForecastReason.QUEUE_UNOBSERVED)
        else:
            reasons.add(ForecastReason.QUEUE_FROM_OUTSTANDING)
    latency_ok = latency_shows_no_queue(data, config)
    if latency_ok:
        reasons.add(ForecastReason.LATENCY_SHOWS_NO_QUEUE)
    if data.late_resolution_hazard_per_s is None:
        reasons.add(ForecastReason.LATE_RESOLUTION_UNOBSERVED)

    base, identified = benefit_lower_bound(data.check_outcomes, config)
    if not identified:
        reasons.add(ForecastReason.CHECK_BENEFIT_UNIDENTIFIED)
    horizon_end = data.decision_time + timedelta(seconds=steps * config.step_s)
    ordered = tuple(c for c in data.candidates if c.eligible_at < horizon_end)
    eligible_now = sum(1 for c in data.candidates if c.eligible_at <= data.decision_time)
    if not ordered:
        reasons.add(ForecastReason.NO_ELIGIBLE_CHECKS)

    def result(budget_forecasts, available, lam):
        if unavailable is not None:
            reasons.add(unavailable)
        return ForecastResult(
            forecast_version=FORECAST_VERSION,
            config_digest=config.digest(),
            experiment_id=data.experiment_id,
            route_id=data.route_id,
            decision_time=data.decision_time,
            horizon_s=steps * config.step_s,
            steps=steps,
            available=available,
            feedback_mode=config.feedback_mode,
            exogenous_payment_rps=lam,
            eligible_now=eligible_now,
            eligible_in_horizon=len(ordered),
            check_benefit_low=base,
            check_benefit_identified=identified,
            budgets=budget_forecasts,
            reasons=tuple(sorted(reasons)),
        )

    if unavailable is not None:
        return result((), False, 0.0)

    lam = max(0.0, cap.offered_load_rps - data.current_check_rps)
    raw_steps = [
        max(
            0,
            math.ceil((c.eligible_at - data.decision_time).total_seconds() / config.step_s - 1e-9),
        )
        for c in ordered
    ]
    # release order is the given order: a later-listed candidate never overtakes an earlier one
    eligible_steps = []
    for s in raw_steps:
        eligible_steps.append(max(s, eligible_steps[-1]) if eligible_steps else s)
    plan = _Plan(
        candidates=ordered,
        benefits=tuple(candidate_benefit(c, base, config) for c in ordered),
        eligible_steps=tuple(eligible_steps),
    )
    capacities = capacity_bands(cap, config, latency_ok)
    base_timeout = (data.observed_timeout_rate or 0.0) if latency_ok else 0.0
    if base_timeout > 0:
        reasons.add(ForecastReason.NONQUEUE_TIMEOUTS)
    per_budget = {
        rate: {
            band: simulate_band(
                data, plan, policy, config, band, capacity, rate, lam, steps, base_timeout
            )
            for band, capacity in capacities.items()
        }
        for rate in rates
    }
    zero = per_budget[0]
    out = []
    for rate in rates:
        bands = per_budget[rate]
        out.append(
            BudgetForecast(
                budget_rps=rate,
                bands=tuple(bands[b] for b in Band),
                incremental_ambiguity=bands[Band.CENTRAL].new_ambiguous
                - zero[Band.CENTRAL].new_ambiguous,
                incremental_ambiguity_pessimistic=bands[Band.PESSIMISTIC].new_ambiguous
                - zero[Band.PESSIMISTIC].new_ambiguous,
                incremental_delay_ms=bands[Band.CENTRAL].predicted_payment_delay_ms
                - zero[Band.CENTRAL].predicted_payment_delay_ms,
            )
        )
    if capacities[Band.CENTRAL] <= 0 or lam >= capacities[Band.CENTRAL]:
        reasons.add(ForecastReason.SATURATED)
    if bands_disagree(out):
        reasons.add(ForecastReason.BAND_DISAGREEMENT)
    return result(tuple(out), True, lam)


def bands_disagree(budgets: Sequence[BudgetForecast]) -> bool:
    """True when the bands rank the budgets differently by net ambiguity (new - answered)."""

    def order(band: Band) -> tuple[int, ...]:
        scored = [
            (
                round(b.band(band).new_ambiguous - b.band(band).expected_authoritative, 6),
                b.budget_rps,
            )
            for b in budgets
        ]
        return tuple(rate for _, rate in sorted(scored))

    return len({order(band) for band in Band}) > 1


# --------------------------------------------------------------------- legacy helper


def choose_budget(capacity_rps: float, payment_rps: float, candidates: list[float]) -> float:
    """Starter scorer kept for the legacy /v1 path; the v1.1 controller uses forecast_budgets."""

    feasible = [
        budget for budget in candidates if budget >= 0 and payment_rps + budget <= capacity_rps
    ]
    return max(feasible, default=0.0)
