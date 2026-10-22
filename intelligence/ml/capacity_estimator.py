"""Trust-aware route capacity estimator (Stage 06).

Question answered: how much extra (discretionary recovery) work can this route safely take now,
given incomplete, possibly stale and possibly manipulated measurements? The estimator never sees
the simulator's true capacity. Its only input is the Track A Route Twin snapshot
(``RouteTwinSnapshot``), whose trusted observation the twin accepted only from a registered
source with ``AUTHENTICATED`` status and no validation flag. Flagged telemetry is never used as a
capacity input. One estimator instance serves exactly one ``(experiment_id, route_id)``.

Identifiability (what the telemetry can and cannot tell)
---------------------------------------------------------
Telemetry is ``REMITTER_OWN_TRAFFIC`` by default: own offered rate ``lambda = payment_rps +
check_rps``, own completed responses ``x = observed_throughput_rps``, own latencies, timeouts and
own outstanding requests ``q = queue_depth``. Traffic of other banks on the same bottleneck is
invisible, so background load is reported as not observable (``None``), never guessed.

* Unsaturated (no backlog): ``x`` follows ``lambda``. Capacity is NOT identified: it can be any
  value above what was actually served. Only a lower bound exists: work that was really served
  in a block of time, ``C >= mean(x over the block)``.
* Saturated (persistent backlog: sojourn ``q / x`` or timeouts above threshold): the bottleneck
  is busy, so ``x`` measures the service rate available to own traffic (capacity minus
  background share). Only then is a point estimate reported.
* Without fresh, consistent, trusted samples nothing is identified, and the safe headroom is 0.

Algorithm ``aorts-capacity/1`` (rates in requests/s, times in s, latencies in s)
---------------------------------------------------------------------------------
Per accepted window ``w`` (duration ``d_w``):

* congestion signal ``s_w`` if any of: ``timeout_count >= min_timeouts`` and ``timeout_rate >=
  timeout_rate_threshold``; sojourn ``q_w / x_w >= sojourn_threshold_s`` (``q_w > 0`` with
  ``x_w = 0`` counts as infinite); ``p95_w >= max(latency_ratio * B, B + latency_excess_s)``
  with baseline ``B`` = minimum p95 of signal-free windows in the last ``baseline_horizon_s``
  (``p95_w >= absolute_latency_s`` while no baseline exists). Latency needs
  ``latency_sample_count >= min_latency_samples``.
* saturation sample if ``s_w`` comes from the sojourn or timeout signal (a backlog, not latency
  alone) and ``x_w > 0``.

Route state (hysteresis): CONGESTED when ``congestion_confirm`` of the last
``congestion_lookback`` windows carry a signal; this sets the regime start, and every capacity
evidence older than the regime start is discarded (a drop cannot hide behind earlier high
throughput). CONGESTED -> RECOVERING after ``congestion_lookback`` signal-free windows;
RECOVERING -> HEALTHY after ``recovery_clean_s`` of consecutive signal-free windows.

Evidence (windows of one source since the regime start and within ``evidence_horizon_s``):

* demonstrated throughput ``D = (1 - lag_allowance_s / sustain_s) x max over contiguous blocks
  of length >= sustain_s of sum(x_w d_w) / sum(d_w)``. Responses lag service by a jittered
  latency, so a block of responses can contain up to ``lag_allowance_s`` worth of service from
  outside the block; the factor removes that. One window moves the max by at most its excess
  times ``d_w / sustain_s``; the rate limit below bounds the rest;
* ``sat`` = saturation-sample throughputs within ``point_horizon_s``; with at least
  ``min_saturation_samples``: point ``C_hat = median(sat)``, ``L_sat = q20(sat) * (1 -
  saturation_margin)`` (nearest-rank 20th percentile);
* raw lower bound ``L_raw = max(D, L_sat)``; ``C_hat`` is dropped (lower bound only) when
  ``C_hat < (1 - saturation_margin) L_raw`` (refuted by served work); otherwise ``L_raw`` is
  capped at ``C_hat``.

Sources: estimates are formed per source and combined by the minimum (most conservative).

Rate limit: the reported lower bound may fall immediately but may rise by at most
``max_increase_per_s * max(L_prev, lambda_bar) * dt`` per elapsed second ``dt``.

Load: ``lambda_bar`` = time-weighted EWMA of own offered rate, ``alpha = 1 - exp(-d_w /
ewma_tau_s)``; the competing load used for headroom is ``max(lambda_bar, lambda_block)`` with
``lambda_block`` the mean offered rate over the latest ``sustain_s``.

Safe headroom: ``H = max(0, rho_max * L - load) * f_mode`` only when the route is HEALTHY, the
newest window carries no congestion signal and the route keeps up with its own load right now
(served ``x_block >= (1 - deficit_tolerance) lambda_block`` over the latest ``sustain_s``: a
capacity drop shows up as a service deficit before the backlog is large enough to signal);
``f_mode`` = 1 (TRUSTED), ``degraded_factor`` (DEGRADED), 0 otherwise. Hence ``0 <= H <=
max(0, L - load)``.

Consistency (conservation) check: every response belongs to one earlier request. Over the
latest contiguous block of ``conservation_block_s``: responses ``<= q(block start) + sends(block)
+ timeouts(prior conservation_lifetime_s) + tolerance``, assuming no response arrives more than
``conservation_lifetime_s`` after its timeout. A violation latches FAIL_CLOSED: by default until
``release_fail_closed`` (a source that broke conservation is not trusted again without operator
action); with ``inconsistency_hold_s`` set, for that long, after which all evidence is discarded
and the estimator re-initialises from fresh samples.
Positive headroom additionally requires that this check was conclusive on the newest window of
every used source: applicable (enough contiguous history with queue and timeout fields), passed,
and tight (the late-response allowance from recent timeouts is within the tolerance; after
heavy timeouts the check is too loose to vouch for anything, so it verifies nothing).
This detects sustained throughput inflation; it cannot detect a sender that fabricates every
field consistently (that is Track B's authentication job), and it is loose under heavy timeouts.

Confidence is a HEURISTIC, UNCALIBRATED score in [0, 1] (product of sample, freshness, gap,
flag and conflict factors). It is not a probability and no interval is claimed calibrated.

Simplifications: windows are treated as rates averaged over their duration; response lag
(beneficiary latency and confirmation delay) is ignored, so a confirmation delay can look like a
backlog (bias is downward, the safe side) and a burst of delayed confirmations can briefly raise
measured throughput above service capacity (contained by the rate limit and ``rho_max``).
"""

import hashlib
import math
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from contracts.v1_1 import (
    FreshnessStatus,
    RouteTelemetry,
    RouteTwinSnapshot,
    SourceSequenceMark,
)
from contracts.v1_1.models import (
    Identifier,
    NonNegativeFloat,
    NonNegativeInt,
    ObservationScope,
    PositiveFloat,
    Probability,
    UtcDatetime,
)

ESTIMATOR_ID = "aorts-capacity"
ESTIMATOR_VERSION = "aorts-capacity/1"
CONFIDENCE_KIND = "HEURISTIC_UNCALIBRATED"


class EstimatorMode(StrEnum):
    INITIALIZING = "INITIALIZING"  # fewer than min_usable_windows since start or reset
    TRUSTED = "TRUSTED"  # fresh, consistent, unflagged evidence from every used source
    DEGRADED = "DEGRADED"  # usable but impaired (gaps, newer flagged sample, conflict, fields)
    STALE = "STALE"  # newest usable sample too old or the twin reports STALE
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"  # samples exist but carry no capacity evidence
    FAIL_CLOSED = "FAIL_CLOSED"  # integrity failure or explicit latch: no headroom


class RouteState(StrEnum):
    UNKNOWN = "UNKNOWN"
    HEALTHY = "HEALTHY"
    CONGESTED = "CONGESTED"
    RECOVERING = "RECOVERING"


class Identifiability(StrEnum):
    IDENTIFIED = "IDENTIFIED"  # point estimate from saturated (busy-server) samples
    LOWER_BOUND_ONLY = "LOWER_BOUND_ONLY"  # only served work bounds capacity from below
    UNIDENTIFIABLE = "UNIDENTIFIABLE"  # no usable evidence


class Reason(StrEnum):
    NO_TRUSTED_OBSERVATION = "NO_TRUSTED_OBSERVATION"
    WARMING_UP = "WARMING_UP"
    STALE_TELEMETRY = "STALE_TELEMETRY"
    TWIN_REPORTS_STALE = "TWIN_REPORTS_STALE"
    SEQUENCE_GAP = "SEQUENCE_GAP"
    REORDERED_SAMPLE_IGNORED = "REORDERED_SAMPLE_IGNORED"
    OVERLAPPING_WINDOW_IGNORED = "OVERLAPPING_WINDOW_IGNORED"
    SCOPE_MISMATCH_IGNORED = "SCOPE_MISMATCH_IGNORED"
    MALFORMED_SAMPLE = "MALFORMED_SAMPLE"
    FUTURE_OBSERVATION = "FUTURE_OBSERVATION"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    CLOCK_REGRESSION = "CLOCK_REGRESSION"
    FLAGGED_TELEMETRY_NEWER = "FLAGGED_TELEMETRY_NEWER"
    SOURCE_CONFLICT = "SOURCE_CONFLICT"
    MISSING_FIELDS = "MISSING_FIELDS"
    CONSERVATION_VIOLATION = "CONSERVATION_VIOLATION"
    EXPLICIT_FAIL_CLOSED = "EXPLICIT_FAIL_CLOSED"
    CONGESTION_SIGNAL = "CONGESTION_SIGNAL"
    ROUTE_NOT_HEALTHY = "ROUTE_NOT_HEALTHY"
    EVIDENCE_RESET_ON_CONGESTION = "EVIDENCE_RESET_ON_CONGESTION"
    CAPACITY_NOT_IDENTIFIABLE = "CAPACITY_NOT_IDENTIFIABLE"
    POINT_REFUTED_BY_SERVED_WORK = "POINT_REFUTED_BY_SERVED_WORK"
    BOUND_RATE_LIMITED = "BOUND_RATE_LIMITED"
    NO_SERVED_WORK = "NO_SERVED_WORK"
    BACKGROUND_LOAD_UNOBSERVABLE = "BACKGROUND_LOAD_UNOBSERVABLE"
    NO_HEADROOM_BELOW_LOAD = "NO_HEADROOM_BELOW_LOAD"
    CONSISTENCY_UNVERIFIED = "CONSISTENCY_UNVERIFIED"
    SERVICE_DEFICIT = "SERVICE_DEFICIT"
    INVARIANT_VIOLATION = "INVARIANT_VIOLATION"


_ZERO_HEADROOM_MODES = frozenset(
    {
        EstimatorMode.INITIALIZING,
        EstimatorMode.STALE,
        EstimatorMode.INSUFFICIENT_DATA,
        EstimatorMode.FAIL_CLOSED,
    }
)


class CapacityEstimatorConfig(BaseModel):
    """Every parameter of ``aorts-capacity/1``; defaults are declared research choices."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    observation_scope: ObservationScope = ObservationScope.REMITTER_OWN_TRAFFIC
    max_observation_age_s: PositiveFloat = 10.0
    min_usable_windows: int = Field(default=10, ge=1, strict=True)
    history_limit_windows: int = Field(default=1200, ge=10, strict=True)
    evidence_horizon_s: PositiveFloat = 600.0
    point_horizon_s: PositiveFloat = 120.0
    sustain_s: PositiveFloat = 10.0
    lag_allowance_s: NonNegativeFloat = 0.5
    min_saturation_samples: int = Field(default=5, ge=1, strict=True)
    saturation_margin: float = Field(default=0.05, ge=0.0, lt=1.0, strict=True)
    min_latency_samples: int = Field(default=3, ge=1, strict=True)
    baseline_horizon_s: PositiveFloat = 600.0
    latency_ratio: float = Field(default=3.0, gt=1.0, strict=True)
    latency_excess_s: PositiveFloat = 1.0
    absolute_latency_s: PositiveFloat = 2.0
    sojourn_threshold_s: PositiveFloat = 1.0
    timeout_rate_threshold: float = Field(default=0.02, gt=0.0, le=1.0, strict=True)
    min_timeouts: int = Field(default=2, ge=1, strict=True)
    congestion_lookback: int = Field(default=3, ge=1, strict=True)
    congestion_confirm: int = Field(default=2, ge=1, strict=True)
    recovery_clean_s: PositiveFloat = 30.0
    max_increase_per_s: PositiveFloat = 0.01
    ewma_tau_s: PositiveFloat = 30.0
    rho_max: float = Field(default=0.85, gt=0.0, le=1.0, strict=True)
    degraded_factor: float = Field(default=0.5, ge=0.0, le=1.0, strict=True)
    conflict_tolerance: float = Field(default=0.2, gt=0.0, strict=True)
    deficit_tolerance: float = Field(default=0.05, ge=0.0, lt=1.0, strict=True)
    conservation_block_s: PositiveFloat = 30.0
    conservation_lifetime_s: PositiveFloat = 60.0
    conservation_tolerance_abs: NonNegativeFloat = 5.0
    conservation_tolerance_rel: float = Field(default=0.03, ge=0.0, strict=True)
    inconsistency_hold_s: PositiveFloat | None = None  # None: until release_fail_closed
    gap_horizon_s: PositiveFloat = 120.0

    @model_validator(mode="after")
    def _consistent(self) -> "CapacityEstimatorConfig":
        if self.congestion_confirm > self.congestion_lookback:
            raise ValueError("congestion_confirm cannot exceed congestion_lookback")
        if self.point_horizon_s > self.evidence_horizon_s:
            raise ValueError("point_horizon_s cannot exceed evidence_horizon_s")
        if self.sustain_s > self.evidence_horizon_s:
            raise ValueError("sustain_s cannot exceed evidence_horizon_s")
        if self.lag_allowance_s >= self.sustain_s:
            raise ValueError("lag_allowance_s must be shorter than sustain_s")
        return self

    def digest(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class CapacityEstimate(BaseModel):
    """Immutable output of one estimator decision. Measured, estimated and unknown stay apart."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    estimator_id: Identifier = ESTIMATOR_ID
    estimator_version: str = ESTIMATOR_VERSION
    config_digest: str
    experiment_id: Identifier
    route_id: Identifier
    decision_time: UtcDatetime
    snapshot_version: NonNegativeInt | None
    observation_watermark: tuple[SourceSequenceMark, ...]
    last_observation_at: UtcDatetime | None
    observation_age_s: NonNegativeFloat | None
    twin_freshness: FreshnessStatus | None
    mode: EstimatorMode
    route_state: RouteState
    identifiability: Identifiability
    # measured (latest accepted window), copied not estimated
    observed_offered_rps: NonNegativeFloat | None
    observed_throughput_rps: NonNegativeFloat | None
    # estimated
    offered_load_rps: NonNegativeFloat | None
    estimated_capacity_rps: NonNegativeFloat | None
    capacity_lower_bound_rps: NonNegativeFloat | None
    utilization_estimate: NonNegativeFloat | None
    utilization_upper_bound: NonNegativeFloat | None
    background_load_rps: NonNegativeFloat | None = None
    safe_headroom_rps: NonNegativeFloat
    confidence: Probability
    confidence_kind: str = CONFIDENCE_KIND
    usable_samples: NonNegativeInt
    saturation_samples: NonNegativeInt
    rejected_samples: NonNegativeInt
    reason_codes: tuple[Reason, ...]

    @model_validator(mode="after")
    def _invariants(self) -> "CapacityEstimate":
        lower, point = self.capacity_lower_bound_rps, self.estimated_capacity_rps
        if point is not None and (lower is None or lower > point):
            raise ValueError("a point estimate needs a lower bound no greater than it")
        if (self.identifiability is Identifiability.IDENTIFIED) != (point is not None):
            raise ValueError("estimated_capacity_rps is present exactly when IDENTIFIED")
        if self.mode in _ZERO_HEADROOM_MODES and self.safe_headroom_rps != 0.0:
            raise ValueError(f"{self.mode} must report zero headroom")
        if self.safe_headroom_rps > 0.0:
            load = self.offered_load_rps
            if lower is None or load is None or self.route_state is not RouteState.HEALTHY:
                raise ValueError("positive headroom needs a bound, a load and a HEALTHY route")
            if self.safe_headroom_rps > lower - load + 1e-9:
                raise ValueError("safe headroom exceeds the lower bound minus competing load")
        if list(self.reason_codes) != sorted(set(self.reason_codes)):
            raise ValueError("reason_codes must be sorted and unique")
        return self

    def digest(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class EstimatorInputError(TypeError):
    """The caller passed something that is not a Route Twin snapshot or an aware datetime."""


# ----------------------------------------------------------------------- pure helpers


def ewma_alpha(dt_s: float, tau_s: float) -> float:
    """Time-weighted EWMA gain: ``1 - exp(-dt / tau)`` (0 for dt <= 0)."""

    if dt_s <= 0:
        return 0.0
    return 1.0 - math.exp(-dt_s / tau_s)


def ewma_update(previous: float | None, sample: float, dt_s: float, tau_s: float) -> float:
    if previous is None:
        return sample
    alpha = ewma_alpha(dt_s, tau_s)
    return previous + alpha * (sample - previous)


def nearest_rank(values: Iterable[float], fraction: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    return ordered[max(1, math.ceil(fraction * len(ordered))) - 1]


def median(values: Iterable[float]) -> float | None:
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        return None
    mid = n // 2
    return ordered[mid] if n % 2 else 0.5 * (ordered[mid - 1] + ordered[mid])


def rate_limited(previous: float | None, raw: float, reference: float, dt_s: float, per_s: float):
    """Fall immediately; rise by at most ``per_s * reference * dt``. Returns (value, limited)."""

    if previous is None or raw <= previous:
        return raw, False
    ceiling = previous + per_s * max(reference, 0.0) * max(dt_s, 0.0)
    return (ceiling, True) if raw > ceiling else (raw, False)


def max_block_mean(windows: list["_Window"], sustain_s: float) -> float | None:
    """Max over contiguous blocks covering >= sustain_s of duration-weighted mean throughput."""

    best: float | None = None
    left, work, span = 0, 0.0, 0.0
    for right, w in enumerate(windows):
        if right > 0 and not _contiguous(windows[right - 1], w):
            left, work, span = right, 0.0, 0.0
        work += w.throughput * w.duration
        span += w.duration
        while left < right and span - windows[left].duration >= sustain_s:
            work -= windows[left].throughput * windows[left].duration
            span -= windows[left].duration
            left += 1
        if span >= sustain_s - 1e-9:
            mean = work / span
            best = mean if best is None else max(best, mean)
    return best


def _contiguous(a: "_Window", b: "_Window") -> bool:
    return b.sequence == a.sequence + 1 and abs(b.start_s - a.end_s) <= 1e-6


# ----------------------------------------------------------------------- internal state


@dataclass(frozen=True, slots=True)
class _Window:
    """Numeric copy of one accepted trusted window (times as seconds since the first window)."""

    source_id: str
    sequence: int
    start_s: float
    end_s: float
    observed_s: float
    offered: float
    throughput: float
    p95_s: float | None
    timeout_count: int | None
    queue_depth: int | None
    timeout_signal: bool
    sojourn_signal: bool
    latency_signal: bool
    missing_fields: bool

    @property
    def duration(self) -> float:
        return self.end_s - self.start_s

    @property
    def signal(self) -> bool:
        return self.timeout_signal or self.sojourn_signal or self.latency_signal

    @property
    def saturated(self) -> bool:
        return (self.timeout_signal or self.sojourn_signal) and self.throughput > 0


@dataclass
class _Source:
    limit: int
    windows: deque = field(default_factory=deque)
    last_sequence: int | None = None
    gaps: deque = field(default_factory=deque)  # (time_s, missing windows)
    verified: bool = False  # conservation check applicable and passed on the newest window

    def add(self, window: _Window) -> None:
        self.windows.append(window)
        while len(self.windows) > self.limit:
            self.windows.popleft()


@dataclass(frozen=True)
class _SourceEvidence:
    demonstrated: float | None
    point: float | None
    sat_lower: float | None
    saturation_samples: int
    usable: int
    block_offered: float | None
    block_throughput: float | None


def _finite_nonneg(value) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _malformed(obs: RouteTelemetry) -> str | None:
    """Re-check what the contract guarantees, so a model built without validation is refused."""

    if not isinstance(obs, RouteTelemetry):
        return "not a RouteTelemetry"
    for name in ("payment_rps", "check_rps"):
        if not _finite_nonneg(getattr(obs, name, None)):
            return f"{name} is not a finite non-negative number"
    for name in ("observed_throughput_rps", "p50_latency_ms", "p95_latency_ms", "timeout_rate"):
        value = getattr(obs, name, None)
        if value is not None and not _finite_nonneg(value):
            return f"{name} is not a finite non-negative number"
    for name in ("latency_sample_count", "timeout_count", "timeout_denominator_count",
                 "queue_depth"):  # fmt: skip
        value = getattr(obs, name, None)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)
                                  or value < 0):  # fmt: skip
            return f"{name} is not a non-negative integer"
    if obs.timeout_rate is not None and obs.timeout_rate > 1:
        return "timeout_rate exceeds 1"
    try:
        start, end, at = (
            obs.observation_window_start,
            obs.observation_window_end,
            obs.observation_timestamp,
        )
        if not (start < end <= at):
            return "window bounds are inconsistent"
    except TypeError:
        return "window timestamps are not comparable datetimes"
    if obs.p50_latency_ms is not None and obs.p95_latency_ms is not None:
        if obs.p95_latency_ms < obs.p50_latency_ms:
            return "p95 latency below p50"
    if (obs.timeout_count is None) != (obs.timeout_denominator_count is None):
        return "timeout count without denominator"
    if obs.timeout_count is not None and obs.timeout_count > obs.timeout_denominator_count:
        return "timeout_count exceeds its denominator"
    return None


class CapacityEstimator:
    """Narrowly stateful estimator for one ``(experiment_id, route_id)``.

    ``update(snapshot, decision_time)`` consumes a Route Twin snapshot taken at the decision
    instant and returns an immutable ``CapacityEstimate``. It never reads a clock, never draws
    random numbers and keeps bounded memory (``history_limit_windows`` per source).
    """

    def __init__(
        self,
        experiment_id: str,
        route_id: str,
        config: CapacityEstimatorConfig | None = None,
    ) -> None:
        self._config = config or CapacityEstimatorConfig()
        if not isinstance(self._config, CapacityEstimatorConfig):
            raise EstimatorInputError("config must be a CapacityEstimatorConfig")
        self._experiment_id = experiment_id
        self._route_id = route_id
        self._config_digest = self._config.digest()
        self._origin: datetime | None = None
        self._last_decision: datetime | None = None
        self._latched_until_s: float | None = None
        self._latched_reason: Reason | None = None
        self._rejected = 0
        self._reset_evidence()

    @property
    def config(self) -> CapacityEstimatorConfig:
        return self._config

    @property
    def key(self) -> tuple[str, str]:
        return (self._experiment_id, self._route_id)

    def _reset_evidence(self) -> None:
        self._sources: dict[str, _Source] = {}
        self._signals: deque[bool] = deque(maxlen=self._config.congestion_lookback)
        self._route_state = RouteState.UNKNOWN
        self._clean_since_s: float | None = None
        self._regime_start_s = -math.inf
        self._baseline: deque[tuple[float, float]] = deque()  # (end_s, p95_s) signal-free
        self._load_ewma: float | None = None
        self._load_time_s: float | None = None
        self._bound: float | None = None
        self._bound_time_s: float | None = None
        self._usable_total = 0
        self._latest: _Window | None = None

    # ------------------------------------------------------------------ public API

    def latch_fail_closed(self) -> None:
        """Explicit operator/controller latch: zero headroom until ``release_fail_closed``."""

        self._latched_until_s = math.inf
        self._latched_reason = Reason.EXPLICIT_FAIL_CLOSED

    def release_fail_closed(self) -> None:
        """Lift a latch. All earlier evidence is discarded: recovery needs fresh evidence."""

        self._latched_until_s = None
        self._latched_reason = None
        self._reset_evidence()

    def update(self, snapshot: RouteTwinSnapshot, decision_time: datetime) -> CapacityEstimate:
        now_dt = _aware(decision_time)
        if not isinstance(snapshot, RouteTwinSnapshot):
            raise EstimatorInputError("update expects a RouteTwinSnapshot")
        if (snapshot.experiment_id, snapshot.route_id) != self.key:
            return self._closed(now_dt, snapshot, Reason.IDENTITY_MISMATCH, record=False)
        if self._last_decision is not None and now_dt < self._last_decision:
            return self._closed(now_dt, snapshot, Reason.CLOCK_REGRESSION, record=False)
        self._last_decision = now_dt
        now = self._seconds(now_dt)

        reasons: set[Reason] = set()
        if self._latched_until_s is not None:
            if now < self._latched_until_s:
                return self._closed(now_dt, snapshot, self._latched_reason, record=True)
            self._latched_until_s, self._latched_reason = None, None
            self._reset_evidence()

        observation = snapshot.latest_trusted_observation
        if observation is not None:
            outcome = self._ingest(observation, now)
            if outcome in (Reason.MALFORMED_SAMPLE, Reason.FUTURE_OBSERVATION):
                self._rejected += 1
                return self._closed(now_dt, snapshot, outcome, record=True)
            if outcome is Reason.CONSERVATION_VIOLATION:
                hold = self._config.inconsistency_hold_s
                self._latched_until_s = math.inf if hold is None else now + hold
                self._latched_reason = Reason.CONSERVATION_VIOLATION
                return self._closed(now_dt, snapshot, outcome, record=True)
            if outcome is not None:
                reasons.add(outcome)
        return self._estimate(snapshot, now_dt, now, reasons)

    # ------------------------------------------------------------------ ingestion

    def _seconds(self, when: datetime) -> float:
        if self._origin is None:
            self._origin = when
        return (when - self._origin).total_seconds()

    def _ingest(self, obs: RouteTelemetry, now: float) -> Reason | None:
        if _malformed(obs) is not None:
            return Reason.MALFORMED_SAMPLE
        if obs.experiment_id != self._experiment_id or obs.route_id != self._route_id:
            return Reason.MALFORMED_SAMPLE
        if obs.observation_scope != self._config.observation_scope:
            return Reason.SCOPE_MISMATCH_IGNORED
        observed = self._seconds(obs.observation_timestamp)
        if observed > now + 1e-9:
            return Reason.FUTURE_OBSERVATION
        source = self._sources.get(obs.source_id)
        if source is None:
            source = self._sources[obs.source_id] = _Source(self._config.history_limit_windows)
        if source.last_sequence is not None:
            if obs.sequence == source.last_sequence:
                return None  # the same sample seen again in a later snapshot: no new data
            if obs.sequence < source.last_sequence:
                return Reason.REORDERED_SAMPLE_IGNORED
        start = self._seconds(obs.observation_window_start)
        end = self._seconds(obs.observation_window_end)
        if source.windows and start < source.windows[-1].end_s - 1e-6:
            return Reason.OVERLAPPING_WINDOW_IGNORED
        if obs.observed_throughput_rps is None:
            source.last_sequence = obs.sequence
            return Reason.MISSING_FIELDS
        if source.last_sequence is not None and obs.sequence > source.last_sequence + 1:
            source.gaps.append((end, obs.sequence - source.last_sequence - 1))
        source.last_sequence = obs.sequence

        window = self._window(obs, start, end, observed)
        source.add(window)
        self._usable_total += 1
        self._latest = window
        self._advance_load(window)
        self._advance_route_state(window)
        if not window.signal and window.p95_s is not None:
            self._baseline.append((window.end_s, window.p95_s))
        while self._baseline and self._baseline[0][0] < end - self._config.baseline_horizon_s:
            self._baseline.popleft()
        consistent = self._conservation(source)
        source.verified = consistent is True
        if consistent is False:
            return Reason.CONSERVATION_VIOLATION
        return Reason.MISSING_FIELDS if window.missing_fields else None

    def _window(self, obs: RouteTelemetry, start: float, end: float, observed: float) -> _Window:
        cfg = self._config
        x = float(obs.observed_throughput_rps)
        q = obs.queue_depth
        timeout_signal = (
            obs.timeout_count is not None
            and obs.timeout_rate is not None
            and obs.timeout_count >= cfg.min_timeouts
            and obs.timeout_rate >= cfg.timeout_rate_threshold
        )
        sojourn_signal = False
        if q is not None and q > 0:
            sojourn_signal = x <= 0 or q / x >= cfg.sojourn_threshold_s
        p95 = None
        if obs.p95_latency_ms is not None and obs.latency_sample_count >= cfg.min_latency_samples:
            p95 = obs.p95_latency_ms / 1000.0
        latency_signal = False
        if p95 is not None:
            base = min((v for _, v in self._baseline), default=None)
            threshold = (
                cfg.absolute_latency_s
                if base is None
                else max(cfg.latency_ratio * base, base + cfg.latency_excess_s)
            )
            latency_signal = p95 >= threshold
        missing = q is None or obs.timeout_count is None or obs.p95_latency_ms is None
        return _Window(
            source_id=obs.source_id,
            sequence=obs.sequence,
            start_s=start,
            end_s=end,
            observed_s=observed,
            offered=float(obs.payment_rps + obs.check_rps),
            throughput=x,
            p95_s=p95,
            timeout_count=obs.timeout_count,
            queue_depth=q,
            timeout_signal=timeout_signal,
            sojourn_signal=sojourn_signal,
            latency_signal=latency_signal,
            missing_fields=missing,
        )

    def _advance_load(self, w: _Window) -> None:
        dt = w.duration if self._load_time_s is None else max(0.0, w.end_s - self._load_time_s)
        self._load_ewma = ewma_update(self._load_ewma, w.offered, dt, self._config.ewma_tau_s)
        self._load_time_s = w.end_s

    def _advance_route_state(self, w: _Window) -> None:
        cfg = self._config
        self._signals.append(w.signal)
        confirmed = sum(self._signals) >= cfg.congestion_confirm
        if confirmed:
            if self._route_state is not RouteState.CONGESTED:
                # every capacity evidence before the onset is no longer trusted
                self._regime_start_s = w.start_s
            self._route_state = RouteState.CONGESTED
            self._clean_since_s = None
            return
        if w.signal:
            self._clean_since_s = None
            return
        if self._clean_since_s is None:
            self._clean_since_s = w.start_s
        clean_for = w.end_s - self._clean_since_s
        if self._route_state is RouteState.CONGESTED:
            if len(self._signals) == cfg.congestion_lookback and not any(self._signals):
                self._route_state = RouteState.RECOVERING
        elif self._route_state is RouteState.UNKNOWN:
            self._route_state = RouteState.HEALTHY
        if self._route_state is RouteState.RECOVERING and clean_for >= cfg.recovery_clean_s:
            self._route_state = RouteState.HEALTHY

    def _conservation(self, source: _Source) -> bool | None:
        """True: consistent and tight; False: violated; None: not applicable or too loose."""

        cfg = self._config
        windows = list(source.windows)
        if not windows:
            return None
        # latest contiguous run
        first = len(windows) - 1
        while first > 0 and _contiguous(windows[first - 1], windows[first]):
            first -= 1
        run = windows[first:]
        end = run[-1].end_s
        block_start = end - cfg.conservation_block_s
        need_from = block_start - cfg.conservation_lifetime_s
        if run[0].start_s > need_from + 1e-9:
            return None  # not enough contiguous history
        block = [w for w in run if w.start_s >= block_start - 1e-9]
        before = [w for w in run if w.end_s <= block[0].start_s + 1e-9] if block else []
        prior = [w for w in before if w.start_s >= block[0].start_s - cfg.conservation_lifetime_s
                 - 1e-9]  # fmt: skip
        if not block or not before:
            return None
        q0 = before[-1].queue_depth
        if q0 is None or any(w.timeout_count is None for w in prior):
            return None
        responses = sum(w.throughput * w.duration for w in block)
        sends = sum(w.offered * w.duration for w in block)
        late = sum(w.timeout_count for w in prior)
        allowance = q0 + sends + late
        tolerance = cfg.conservation_tolerance_abs + cfg.conservation_tolerance_rel * sends
        if responses > allowance + tolerance:
            return False
        return True if late <= tolerance else None  # loose after heavy timeouts: inconclusive

    # ------------------------------------------------------------------ estimation

    def _evidence(self, source: _Source, now: float) -> _SourceEvidence:
        cfg = self._config
        horizon = max(self._regime_start_s, now - cfg.evidence_horizon_s)
        windows = [w for w in source.windows if w.start_s >= horizon - 1e-9]
        demonstrated = max_block_mean(windows, cfg.sustain_s)
        if demonstrated is not None:
            demonstrated *= 1.0 - cfg.lag_allowance_s / cfg.sustain_s
        point_from = max(self._regime_start_s, now - cfg.point_horizon_s)
        sat = [w.throughput for w in windows if w.saturated and w.start_s >= point_from - 1e-9]
        point = sat_lower = None
        if len(sat) >= cfg.min_saturation_samples:
            point = median(sat)
            sat_lower = nearest_rank(sat, 0.2) * (1.0 - cfg.saturation_margin)
        recent = [w for w in windows if w.end_s >= now - cfg.sustain_s - 1e-9]
        span = sum(w.duration for w in recent)
        block_offered = sum(w.offered * w.duration for w in recent) / span if span else None
        block_served = sum(w.throughput * w.duration for w in recent) / span if span else None
        return _SourceEvidence(demonstrated, point, sat_lower, len(sat), len(windows),
                               block_offered, block_served)  # fmt: skip

    def _estimate(
        self,
        snapshot: RouteTwinSnapshot,
        now_dt: datetime,
        now: float,
        reasons: set[Reason],
    ) -> CapacityEstimate:
        cfg = self._config
        latest = self._latest
        if latest is None:
            reasons.add(Reason.NO_TRUSTED_OBSERVATION)
            return self._build(snapshot, now_dt, EstimatorMode.INITIALIZING, reasons)

        evidence = {sid: self._evidence(src, now) for sid, src in sorted(self._sources.items())}
        fresh = {
            sid: ev
            for sid, ev in evidence.items()
            if self._sources[sid].windows
            and now - self._sources[sid].windows[-1].observed_s <= cfg.max_observation_age_s
        }
        age = max(0.0, now - max(src.windows[-1].observed_s for src in self._sources.values()
                                 if src.windows))  # fmt: skip

        stale = not fresh
        if stale:
            reasons.add(Reason.STALE_TELEMETRY)
        if snapshot.freshness_status is not FreshnessStatus.FRESH:
            reasons.add(Reason.TWIN_REPORTS_STALE)
            stale = True
        used = fresh or evidence

        bounds = [_raw_bound(ev) for ev in used.values()]
        raw = min((b for b in bounds if b is not None), default=None)
        points = [ev.point for ev in used.values()]
        point = min(points) if points and all(p is not None for p in points) else None
        if point is not None and raw is not None:
            if point < (1.0 - cfg.saturation_margin) * raw:
                reasons.add(Reason.POINT_REFUTED_BY_SERVED_WORK)
                point = None
            else:
                raw = min(raw, point)
        saturation = sum(ev.saturation_samples for ev in used.values())
        usable = sum(ev.usable for ev in used.values())

        degraded = False
        if len(used) > 1:
            demos = [ev.demonstrated for ev in used.values() if ev.demonstrated is not None]
            if demos and min(demos) > 0 and max(demos) / min(demos) - 1 > cfg.conflict_tolerance:
                reasons.add(Reason.SOURCE_CONFLICT)
                degraded = True
        flagged = snapshot.latest_flagged_observation
        if flagged is not None and self._seconds(flagged.telemetry.observation_timestamp) > (
            latest.observed_s + 1e-9
        ):
            reasons.add(Reason.FLAGGED_TELEMETRY_NEWER)
            degraded = True
        gap_missing, gap_expected = self._gap_fraction(now)
        if gap_missing:
            reasons.add(Reason.SEQUENCE_GAP)
            degraded = True
        if latest.missing_fields or Reason.MISSING_FIELDS in reasons:
            reasons.add(Reason.MISSING_FIELDS)
            degraded = True

        if self._route_state is RouteState.CONGESTED and self._regime_start_s > -math.inf:
            reasons.add(Reason.EVIDENCE_RESET_ON_CONGESTION)

        # lower bound with asymmetric rate limit (frozen, never raised, while degraded/stale)
        bound = raw
        if raw is not None:
            if raw == 0.0:
                reasons.add(Reason.NO_SERVED_WORK)
            prev = self._bound
            if prev is not None and (degraded or stale) and raw > prev:
                bound = prev
            else:
                dt = 0.0 if self._bound_time_s is None else now - self._bound_time_s
                bound, limited = rate_limited(
                    prev, raw, max(prev or 0.0, self._load_ewma or 0.0), dt,
                    cfg.max_increase_per_s,
                )  # fmt: skip
                if limited:
                    reasons.add(Reason.BOUND_RATE_LIMITED)
            if not stale:
                self._bound, self._bound_time_s = bound, now

        if point is not None and (bound is None or point < bound):
            point = None  # only reachable when a frozen bound sits above a fresh point

        if self._usable_total < cfg.min_usable_windows:
            mode = EstimatorMode.INITIALIZING
            reasons.add(Reason.WARMING_UP)
        elif stale:
            mode = EstimatorMode.STALE
        elif bound is None or bound == 0.0:
            mode = EstimatorMode.INSUFFICIENT_DATA
        elif degraded:
            mode = EstimatorMode.DEGRADED
        else:
            mode = EstimatorMode.TRUSTED

        if point is None:
            reasons.add(Reason.CAPACITY_NOT_IDENTIFIABLE)
        if cfg.observation_scope is ObservationScope.REMITTER_OWN_TRAFFIC:
            reasons.add(Reason.BACKGROUND_LOAD_UNOBSERVABLE)

        block_offered = [ev.block_offered for ev in used.values() if ev.block_offered is not None]
        load = max([self._load_ewma or 0.0, *block_offered])

        verified = all(self._sources[sid].verified for sid in used)
        if not verified:
            reasons.add(Reason.CONSISTENCY_UNVERIFIED)
        keeping_up = all(
            ev.block_offered is not None
            and ev.block_throughput is not None
            and ev.block_throughput >= (1.0 - cfg.deficit_tolerance) * ev.block_offered
            for ev in used.values()
        )
        if not keeping_up:
            reasons.add(Reason.SERVICE_DEFICIT)
        headroom = 0.0
        if latest.signal:
            reasons.add(Reason.CONGESTION_SIGNAL)
        if self._route_state is not RouteState.HEALTHY:
            reasons.add(Reason.ROUTE_NOT_HEALTHY)
        if (
            mode in (EstimatorMode.TRUSTED, EstimatorMode.DEGRADED)
            and self._route_state is RouteState.HEALTHY
            and not latest.signal
            and verified
            and keeping_up
            and bound is not None
        ):
            factor = 1.0 if mode is EstimatorMode.TRUSTED else cfg.degraded_factor
            headroom = max(0.0, cfg.rho_max * bound - load) * factor
            if headroom == 0.0:
                reasons.add(Reason.NO_HEADROOM_BELOW_LOAD)

        confidence = self._confidence(mode, usable, age, gap_missing, gap_expected, reasons)
        return self._build(
            snapshot,
            now_dt,
            mode,
            reasons,
            age=age,
            point=point,
            bound=bound,
            load=load,
            headroom=headroom,
            confidence=confidence,
            usable=usable,
            saturation=saturation,
        )

    def _gap_fraction(self, now: float) -> tuple[int, int]:
        horizon = now - self._config.gap_horizon_s
        missing = received = 0
        for source in self._sources.values():
            while source.gaps and source.gaps[0][0] < horizon:
                source.gaps.popleft()
            missing += sum(n for _, n in source.gaps)
            received += sum(1 for w in source.windows if w.end_s >= horizon)
        return missing, missing + received

    def _confidence(self, mode, usable, age, gap_missing, gap_expected, reasons) -> float:
        cfg = self._config
        if mode is EstimatorMode.FAIL_CLOSED:
            return 0.0
        samples = min(1.0, usable / max(cfg.min_usable_windows * 3, 1))
        freshness = max(0.0, 1.0 - age / cfg.max_observation_age_s)
        gaps = 1.0 - (gap_missing / gap_expected if gap_expected else 0.0)
        penalty = 1.0
        for reason in (Reason.FLAGGED_TELEMETRY_NEWER, Reason.SOURCE_CONFLICT,
                       Reason.MISSING_FIELDS):  # fmt: skip
            if reason in reasons:
                penalty *= 0.5
        return round(min(1.0, max(0.0, samples * freshness * gaps * penalty)), 6)

    def _closed(self, now_dt, snapshot, reason, record: bool) -> CapacityEstimate:
        same_route = (snapshot.experiment_id, snapshot.route_id) == self.key
        return self._build(
            snapshot if same_route else None,
            now_dt,
            EstimatorMode.FAIL_CLOSED,
            {reason},
            keep_measured=record and same_route,
        )

    def _build(
        self,
        snapshot: RouteTwinSnapshot | None,
        now_dt: datetime,
        mode: EstimatorMode,
        reasons: set[Reason],
        *,
        age: float | None = None,
        point: float | None = None,
        bound: float | None = None,
        load: float | None = None,
        headroom: float = 0.0,
        confidence: float = 0.0,
        usable: int = 0,
        saturation: int = 0,
        keep_measured: bool = True,
    ) -> CapacityEstimate:
        latest = self._latest if keep_measured else None
        if mode in _ZERO_HEADROOM_MODES:
            headroom = 0.0
            point = None if mode is EstimatorMode.FAIL_CLOSED else point
            bound = None if mode is EstimatorMode.FAIL_CLOSED else bound
        identifiability = (
            Identifiability.IDENTIFIED
            if point is not None
            else Identifiability.LOWER_BOUND_ONLY
            if bound
            else Identifiability.UNIDENTIFIABLE
        )
        last_at = None
        if snapshot is not None and snapshot.latest_trusted_observation is not None:
            last_at = snapshot.latest_trusted_observation.observation_timestamp
        try:
            return CapacityEstimate(
                config_digest=self._config_digest,
                experiment_id=self._experiment_id,
                route_id=self._route_id,
                decision_time=now_dt,
                snapshot_version=snapshot.snapshot_version if snapshot is not None else None,
                observation_watermark=(
                    snapshot.latest_sequence_per_source if snapshot is not None else ()
                ),
                last_observation_at=last_at,
                observation_age_s=age,
                twin_freshness=snapshot.freshness_status if snapshot is not None else None,
                mode=mode,
                route_state=self._route_state,
                identifiability=identifiability,
                observed_offered_rps=latest.offered if latest else None,
                observed_throughput_rps=latest.throughput if latest else None,
                offered_load_rps=load,
                estimated_capacity_rps=point,
                capacity_lower_bound_rps=bound,
                utilization_estimate=(load / point) if point and load is not None else None,
                utilization_upper_bound=(load / bound) if bound and load is not None else None,
                safe_headroom_rps=headroom,
                confidence=confidence,
                usable_samples=usable,
                saturation_samples=saturation,
                rejected_samples=self._rejected,
                reason_codes=tuple(sorted(reasons)),
            )
        except ValidationError:  # an output invariant broke: never emit headroom
            if mode is EstimatorMode.FAIL_CLOSED:
                raise
            return self._build(
                snapshot, now_dt, EstimatorMode.FAIL_CLOSED, {Reason.INVARIANT_VIOLATION},
                keep_measured=False,
            )  # fmt: skip


def _raw_bound(ev: _SourceEvidence) -> float | None:
    candidates = [v for v in (ev.demonstrated, ev.sat_lower) if v is not None]
    return max(candidates) if candidates else None


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise EstimatorInputError("decision_time must be a timezone-aware datetime")
    return value


def annotate_snapshot(snapshot: RouteTwinSnapshot, estimate: CapacityEstimate) -> RouteTwinSnapshot:
    """Return a new snapshot carrying the estimate in the contract's existing estimate fields.

    Uses only fields v1.1 already defines (no contract change). The input is not modified and
    the result is fully re-validated by the contract model.
    """

    if (snapshot.experiment_id, snapshot.route_id) != (estimate.experiment_id, estimate.route_id):
        raise EstimatorInputError("estimate belongs to a different route")
    data = snapshot.model_dump()
    data.update(
        estimated_capacity_rps=estimate.estimated_capacity_rps,
        capacity_lower_bound_rps=estimate.capacity_lower_bound_rps,
        telemetry_confidence=estimate.confidence,
        estimator_id=estimate.estimator_id,
    )
    return RouteTwinSnapshot.model_validate(data)
