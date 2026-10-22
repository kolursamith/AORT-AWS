"""Safe recovery-budget optimizer: Stage 06 headroom + Stage 07 look-ahead -> v1.1 decision.

``decide`` turns one route's observable state into a strict v1.1 ``RecoveryDecision``. It is a
pure function of its inputs (no clock, no randomness, no I/O) and never moves money: a decision
only bounds how many status checks Track B's execution gate may admit, and the gate re-checks
every selected transaction against authoritative state.

Order of evaluation
-------------------
1. Identity: the snapshot, estimate and pool must belong to the decision's route and
   experiment (``OptimizerInputError`` otherwise; the service answers with a refusal).
2. Fail-closed and warm-up (every ablation): estimator FAIL_CLOSED -> FAIL_CLOSED
   ``TELEMETRY_UNTRUSTED`` (integrity reasons) or ``NO_CAPACITY_ESTIMATE``; STALE -> FAIL_CLOSED
   ``TELEMETRY_STALE``; INSUFFICIENT_DATA -> FAIL_CLOSED ``NO_CAPACITY_ESTIMATE``; INITIALIZING
   -> WARMING_UP. These decisions carry zero budget and no selection.
3. No transaction eligible now -> NORMAL ``NO_ELIGIBLE_TRANSACTIONS``, zero budget.
4. Budget cap (checks/s, integer): the estimator's conservative ``safe_headroom_rps``, floored,
   and ``max_recovery_rps``. The two research-only ablations replace the headroom cap (see
   below). The per-decision selection is further limited by ``floor(b x validity_s)``, the
   eligible count, the remitter bank's remaining quota and any credit allowance Track B has
   actually provided (``None`` means none was provided, never an invented number). Cap < 1 ->
   NORMAL ``NO_SAFE_BUDGET``.
5. Candidates ``{0} + {ladder <= cap} + {cap}`` are forecast together (Stage 07, same
   observable assumptions, the ranked pool as release order) and scored:

   ``J(b) = w_amb N + w_backlog B_end + w_delay (P x W / 60) + w_deadline M + w_check C
   - w_res R``

   in transaction-equivalents: ``N`` new ambiguous payments, ``B_end`` ambiguous backlog at the
   horizon, ``P x W / 60`` payment-minutes of queueing delay, ``M`` expected deadline misses,
   ``C`` checks released, ``R`` expected authoritative answers (resolution enters directly and
   through ``B_end``). The PESSIMISTIC capacity band is used (CENTRAL in the no-uncertainty
   ablation). A positive budget is chosen only if it improves on zero by at least
   ``min_improvement`` and, unless disabled, also improves in the CENTRAL band (so band
   disagreement can only lead to zero). Ties go to the smaller budget. Weight profiles are
   research choices; none is claimed universally optimal.
6. The decision selects the first eligible ranked transactions, with their projection versions.

Ablations (``Ablation``): FULL; NO_LOOKAHEAD (budget = cap, no forecast); NO_FEEDBACK (forecast
without the check -> timeout feedback); NO_DEADLINE_RANKING (age order); and two research-only
references that are refused unless ``allow_research_ablations`` is set: NO_UNCERTAINTY_BOUND
(cap from the central capacity, ``rho_max x C - load``, and CENTRAL-band scoring) and
FIXED_BUDGET (``fixed_budget_rps`` whenever anything is eligible). Every ablation keeps steps 1-3,
eligibility, state versions, quotas, credit limits and the contract invariants.
"""

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from contracts.v1_1 import (
    SCHEMA_VERSION,
    DecisionMode,
    DecisionReason,
    RecoveryDecision,
    RouteTwinSnapshot,
    TimingPolicy,
    TransactionVersionReference,
)
from contracts.v1_1.models import NonNegativeFloat, PositiveFloat
from intelligence.controller.lookahead import (
    Band,
    BandForecast,
    CheckOutcomeCounts,
    FeedbackMode,
    ForecastInput,
    ForecastResult,
    LookaheadConfig,
    benefit_lower_bound,
    forecast_budgets,
)
from intelligence.controller.ranker import PoolEntry, rank_pool, select_for_dispatch
from intelligence.ml.capacity_estimator import (
    CapacityEstimate,
    EstimatorMode,
    Identifiability,
    Reason,
)

POLICY_VERSION = "aorts-optimizer.1"
MODEL_VERSION = "aorts-capacity.1+aorts-lookahead.1"

_INTEGRITY_REASONS = frozenset(
    {
        Reason.CONSERVATION_VIOLATION,
        Reason.MALFORMED_SAMPLE,
        Reason.IDENTITY_MISMATCH,
        Reason.FUTURE_OBSERVATION,
        Reason.CLOCK_REGRESSION,
        Reason.EXPLICIT_FAIL_CLOSED,
        Reason.INVARIANT_VIOLATION,
    }
)


class Ablation(StrEnum):
    FULL = "FULL"
    NO_LOOKAHEAD = "NO_LOOKAHEAD"
    NO_FEEDBACK = "NO_FEEDBACK"
    NO_DEADLINE_RANKING = "NO_DEADLINE_RANKING"
    NO_UNCERTAINTY_BOUND = "NO_UNCERTAINTY_BOUND"  # research only
    FIXED_BUDGET = "FIXED_BUDGET"  # research reference only


RESEARCH_ONLY = frozenset({Ablation.NO_UNCERTAINTY_BOUND, Ablation.FIXED_BUDGET})


class OptimizerInputError(ValueError):
    """Inputs that do not describe one route of one experiment at one instant."""


class WeightProfile(BaseModel):
    """Objective weights in transaction-equivalents (see the module docstring)."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    name: str = Field(min_length=1, max_length=40)
    ambiguity: NonNegativeFloat
    backlog: NonNegativeFloat
    delay_per_payment_minute: NonNegativeFloat
    deadline: NonNegativeFloat
    check: NonNegativeFloat
    resolution: NonNegativeFloat


PROFILES: dict[str, WeightProfile] = {
    "balanced": WeightProfile(
        name="balanced",
        ambiguity=1.0,
        backlog=0.5,
        delay_per_payment_minute=0.1,
        deadline=2.0,
        check=0.02,
        resolution=1.0,
    ),
    "ambiguity_averse": WeightProfile(
        name="ambiguity_averse",
        ambiguity=3.0,
        backlog=0.5,
        delay_per_payment_minute=0.3,
        deadline=2.0,
        check=0.05,
        resolution=1.0,
    ),
    "recovery_seeking": WeightProfile(
        name="recovery_seeking",
        ambiguity=0.5,
        backlog=1.0,
        delay_per_payment_minute=0.05,
        deadline=3.0,
        check=0.0,
        resolution=2.0,
    ),
}


class OptimizerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    profile: WeightProfile = PROFILES["balanced"]
    ablation: Ablation = Ablation.FULL
    allow_research_ablations: bool = False
    decision_validity_s: PositiveFloat = 5.0
    max_recovery_rps: int = Field(default=20, ge=0, strict=True)
    budget_ladder: tuple[int, ...] = (1, 2, 3, 5, 8, 13, 20)
    fixed_budget_rps: int = Field(default=3, ge=0, strict=True)
    min_improvement: NonNegativeFloat = 0.5
    require_central_improvement: bool = True
    rho_max: float = Field(default=0.85, gt=0.0, le=1.0, strict=True)
    lookahead: LookaheadConfig = LookaheadConfig()

    def digest(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


@dataclass(frozen=True)
class RouteObservables:
    """Observable route facts beyond the capacity estimate (all remitter-visible)."""

    observed_outstanding: int | None = None
    observed_median_latency_s: float | None = None
    observed_timeout_rate: float | None = None
    late_resolution_hazard_per_s: float | None = None
    ambiguous_backlog: int = 0
    current_check_rps: float = 0.0
    check_outcomes: CheckOutcomeCounts = field(default_factory=CheckOutcomeCounts)


@dataclass(frozen=True)
class DecisionContext:
    experiment_id: str
    route_id: str
    decision_time: datetime
    snapshot: RouteTwinSnapshot
    capacity: CapacityEstimate
    pool: tuple[PoolEntry, ...]
    observables: RouteObservables
    policy: TimingPolicy
    bank_quota: Mapping[str, int] | None = None
    credit_allowance: int | None = None


@dataclass(frozen=True)
class CandidateEvaluation:
    budget_rps: int
    selection_limit: int
    binding: tuple[str, ...]
    terms: tuple[tuple[str, float], ...]
    objective: float | None
    objective_central: float | None


@dataclass(frozen=True)
class OptimizationResult:
    decision: RecoveryDecision
    ablation: Ablation
    profile: str
    research_only: bool
    cap_rps: int
    selected_budget_rps: int
    evaluations: tuple[CandidateEvaluation, ...]
    forecast: ForecastResult | None
    reasons: tuple[str, ...]
    capacity_confidence: float


# ----------------------------------------------------------------- objective


def objective_terms(band: BandForecast, weights: WeightProfile) -> dict[str, float]:
    payment_minutes = band.payment_arrivals * band.mean_payment_wait_s / 60.0
    return {
        "new_ambiguity": weights.ambiguity * band.new_ambiguous,
        "backlog": weights.backlog * band.backlog_end,
        "delay": weights.delay_per_payment_minute * payment_minutes,
        "deadline": weights.deadline * band.deadline_misses,
        "check": weights.check * band.checks_released,
        "resolution_benefit": -weights.resolution * band.expected_authoritative,
    }


def objective(band: BandForecast, weights: WeightProfile) -> float:
    return sum(objective_terms(band, weights).values())


# ----------------------------------------------------------------- the decision


def _decision_id(ctx: DecisionContext, config: OptimizerConfig, budget: int, ids) -> str:
    payload = "|".join(
        [
            ctx.experiment_id,
            ctx.route_id,
            ctx.decision_time.isoformat(),
            str(ctx.snapshot.snapshot_version),
            config.digest(),
            str(budget),
            *ids,
        ]
    )
    return "dec-" + hashlib.sha256(payload.encode()).hexdigest()[:40]


def _policy_version(config: OptimizerConfig) -> str:
    return f"{POLICY_VERSION}+{config.profile.name}+{config.ablation.value.lower()}"


def _build(
    ctx: DecisionContext,
    config: OptimizerConfig,
    mode: DecisionMode,
    reason: DecisionReason,
    detail: str,
    budget: int = 0,
    selected: tuple[PoolEntry, ...] = (),
    predicted: BandForecast | None = None,
    horizon_s: float | None = None,
) -> RecoveryDecision:
    ids = tuple(e.transaction_id for e in selected)
    return RecoveryDecision(
        schema_version=SCHEMA_VERSION,
        experiment_id=ctx.experiment_id,
        route_id=ctx.route_id,
        decision_id=_decision_id(ctx, config, budget, ids),
        policy_version=_policy_version(config),
        model_version=MODEL_VERSION,
        timing_policy_id=ctx.policy.policy_id,
        created_at=ctx.decision_time,
        expires_at=ctx.decision_time + timedelta(seconds=config.decision_validity_s),
        decision_mode=mode,
        budget_rps=budget,
        selected_transaction_ids=ids,
        state_versions=tuple(
            TransactionVersionReference(
                transaction_id=e.transaction_id, version=e.candidate.version
            )
            for e in selected
        ),
        predicted_delay_ms=predicted.predicted_payment_delay_ms if predicted else None,
        predicted_new_ambiguous=predicted.new_ambiguous if predicted else None,
        prediction_horizon_s=horizon_s if predicted else None,
        reason_code=reason,
        reason_detail=detail[:1000],
        telemetry_watermark=ctx.snapshot.latest_sequence_per_source,
        latest_observation_at=ctx.snapshot.last_observed_at,
    )


def _validate(ctx: DecisionContext, config: OptimizerConfig) -> None:
    key = (ctx.experiment_id, ctx.route_id)
    if (ctx.snapshot.experiment_id, ctx.snapshot.route_id) != key:
        raise OptimizerInputError("snapshot belongs to another route or experiment")
    if (ctx.capacity.experiment_id, ctx.capacity.route_id) != key:
        raise OptimizerInputError("capacity estimate belongs to another route or experiment")
    if ctx.decision_time.tzinfo is None:
        raise OptimizerInputError("decision_time must be timezone-aware")
    seen = set()
    for entry in ctx.pool:
        c = entry.candidate
        if (c.experiment_id, c.route_id) != key:
            raise OptimizerInputError(f"{c.transaction_id} belongs to another route")
        if c.transaction_id in seen:
            raise OptimizerInputError(f"{c.transaction_id} is in the pool twice")
        seen.add(c.transaction_id)
    if ctx.credit_allowance is not None and ctx.credit_allowance < 0:
        raise OptimizerInputError("credit_allowance cannot be negative")
    if config.ablation in RESEARCH_ONLY and not config.allow_research_ablations:
        raise OptimizerInputError(f"{config.ablation} is a research-only ablation")


def _gatekeeping(ctx: DecisionContext, config: OptimizerConfig) -> RecoveryDecision | None:
    cap = ctx.capacity
    if cap.mode is EstimatorMode.FAIL_CLOSED:
        untrusted = bool(set(cap.reason_codes) & _INTEGRITY_REASONS)
        reason = (
            DecisionReason.TELEMETRY_UNTRUSTED
            if untrusted
            else (DecisionReason.NO_CAPACITY_ESTIMATE)
        )
        return _build(
            ctx,
            config,
            DecisionMode.FAIL_CLOSED,
            reason,
            f"capacity estimator failed closed: {_codes(cap)}",
        )
    if cap.mode is EstimatorMode.STALE:
        return _build(
            ctx,
            config,
            DecisionMode.FAIL_CLOSED,
            DecisionReason.TELEMETRY_STALE,
            f"telemetry is stale ({cap.observation_age_s} s old): {_codes(cap)}",
        )
    if cap.mode is EstimatorMode.INITIALIZING:
        return _build(
            ctx,
            config,
            DecisionMode.WARMING_UP,
            DecisionReason.CONTROLLER_WARMING_UP,
            f"estimator initializing with {cap.usable_samples} samples",
        )
    if cap.mode is EstimatorMode.INSUFFICIENT_DATA or cap.capacity_lower_bound_rps is None:
        return _build(
            ctx,
            config,
            DecisionMode.FAIL_CLOSED,
            DecisionReason.NO_CAPACITY_ESTIMATE,
            f"no capacity evidence: {_codes(cap)}",
        )
    return None


def _codes(cap: CapacityEstimate) -> str:
    return ",".join(r.value for r in cap.reason_codes) or "none"


def _cap(ctx: DecisionContext, config: OptimizerConfig) -> tuple[int, tuple[str, ...]]:
    cap = ctx.capacity
    binding = []
    if config.ablation is Ablation.FIXED_BUDGET:
        raw = config.fixed_budget_rps
        binding.append("fixed_budget")
    elif config.ablation is Ablation.NO_UNCERTAINTY_BOUND:
        if cap.identifiability is Identifiability.IDENTIFIED and cap.estimated_capacity_rps:
            central = cap.estimated_capacity_rps
        else:
            central = (cap.capacity_lower_bound_rps or 0.0) * (
                config.lookahead.optimistic_capacity_factor
            )
        raw = math.floor(max(0.0, config.rho_max * central - (cap.offered_load_rps or 0.0)))
        binding.append("central_capacity_headroom")
    else:
        raw = math.floor(cap.safe_headroom_rps + 1e-9)
        binding.append("conservative_headroom")
    if raw > config.max_recovery_rps:
        raw = config.max_recovery_rps
        binding = ["max_recovery_rps"]
    return max(0, int(raw)), tuple(binding)


def _selection_limit(
    ctx: DecisionContext, config: OptimizerConfig, budget: int, eligible_now: int
) -> tuple[int, tuple[str, ...]]:
    limits = {
        "validity_window": math.floor(budget * config.decision_validity_s + 1e-9),
        "eligible_transactions": eligible_now,
    }
    if ctx.bank_quota is not None:
        banks = {e.candidate.remitter_bank_id or "" for e in ctx.pool}
        limits["bank_quota"] = sum(max(0, ctx.bank_quota.get(b, 0)) for b in banks)
    if ctx.credit_allowance is not None:
        limits["credit_allowance"] = ctx.credit_allowance
    limit = min(limits.values())
    return limit, tuple(sorted(k for k, v in limits.items() if v == limit))


def decide(ctx: DecisionContext, config: OptimizerConfig | None = None) -> OptimizationResult:
    config = config or OptimizerConfig()
    _validate(ctx, config)
    research = config.ablation in RESEARCH_ONLY
    base, _ = benefit_lower_bound(ctx.observables.check_outcomes, config.lookahead)
    ranked = rank_pool(
        ctx.pool,
        base,
        config.lookahead,
        deadline_ranking=config.ablation is not Ablation.NO_DEADLINE_RANKING,
    )
    now = ctx.decision_time

    def result(decision, cap_rps=0, evaluations=(), forecast=None, reasons=()):
        return OptimizationResult(
            decision=decision,
            ablation=config.ablation,
            profile=config.profile.name,
            research_only=research,
            cap_rps=cap_rps,
            selected_budget_rps=decision.budget_rps,
            evaluations=tuple(evaluations),
            forecast=forecast,
            reasons=tuple(reasons),
            capacity_confidence=ctx.capacity.confidence,
        )

    gated = _gatekeeping(ctx, config)
    if gated is not None:
        return result(gated, reasons=(gated.reason_code.value,))
    eligible_now = sum(1 for e in ranked if e.candidate.eligible_at <= now)
    if eligible_now == 0:
        d = _build(
            ctx,
            config,
            DecisionMode.NORMAL,
            DecisionReason.NO_ELIGIBLE_TRANSACTIONS,
            f"no transaction eligible now ({len(ranked)} later in the pool)",
        )
        return result(d, reasons=("no_eligible_transactions",))

    cap_rps, cap_binding = _cap(ctx, config)
    if cap_rps < 1:
        d = _build(
            ctx,
            config,
            DecisionMode.NORMAL,
            DecisionReason.NO_SAFE_BUDGET,
            f"budget cap {cap_rps} rps ({', '.join(cap_binding)}); route state "
            f"{ctx.capacity.route_state}, headroom {ctx.capacity.safe_headroom_rps:.3f}",
        )
        return result(d, cap_rps, reasons=("cap_below_one", *cap_binding))

    budgets = sorted({b for b in config.budget_ladder if b <= cap_rps} | {cap_rps})

    # fixed references: no forecast
    if config.ablation in (Ablation.NO_LOOKAHEAD, Ablation.FIXED_BUDGET):
        return _finish(
            ctx,
            config,
            ranked,
            cap_rps,
            cap_binding,
            cap_rps,
            None,
            (),
            eligible_now,
            result,
            "no look-ahead: budget = cap",
        )

    mode = (
        FeedbackMode.NO_FEEDBACK
        if config.ablation is Ablation.NO_FEEDBACK
        else (config.lookahead.feedback_mode)
    )
    la_config = config.lookahead.model_copy(update={"feedback_mode": mode})
    obs = ctx.observables
    forecast = forecast_budgets(
        ForecastInput(
            experiment_id=ctx.experiment_id,
            route_id=ctx.route_id,
            decision_time=now,
            capacity=ctx.capacity,
            observed_outstanding=obs.observed_outstanding,
            current_check_rps=obs.current_check_rps,
            ambiguous_backlog=obs.ambiguous_backlog,
            late_resolution_hazard_per_s=obs.late_resolution_hazard_per_s,
            candidates=tuple(e.candidate for e in ranked),
            check_outcomes=obs.check_outcomes,
            observed_median_latency_s=obs.observed_median_latency_s,
            observed_timeout_rate=obs.observed_timeout_rate,
        ),
        budgets,
        ctx.policy,
        la_config,
    )
    if not forecast.available:
        d = _build(
            ctx,
            config,
            DecisionMode.FAIL_CLOSED,
            DecisionReason.NO_CAPACITY_ESTIMATE,
            f"forecast unavailable: {','.join(r.value for r in forecast.reasons)}",
        )
        return result(d, cap_rps, forecast=forecast, reasons=("forecast_unavailable",))

    band = Band.CENTRAL if config.ablation is Ablation.NO_UNCERTAINTY_BOUND else Band.PESSIMISTIC
    evaluations = []
    scores = {}
    for b in [0, *budgets]:
        fb = forecast.budget(b)
        limit, limit_binding = _selection_limit(ctx, config, b, eligible_now)
        terms = objective_terms(fb.band(band), config.profile)
        j = sum(terms.values())
        jc = objective(fb.band(Band.CENTRAL), config.profile)
        scores[b] = (j, jc)
        evaluations.append(
            CandidateEvaluation(
                budget_rps=b,
                selection_limit=limit,
                binding=(cap_binding if b == cap_rps else ()) + limit_binding,
                terms=tuple(sorted(terms.items())),
                objective=j,
                objective_central=jc,
            )
        )
    zero_j, zero_jc = scores[0]
    best = 0
    for b in budgets:
        j, jc = scores[b]
        improves = zero_j - j >= config.min_improvement
        if config.require_central_improvement and band is Band.PESSIMISTIC:
            improves = improves and jc < zero_jc
        if improves and j < scores[best][0] - 1e-12:
            best = b
    detail = (
        f"{config.profile.name}: J(0)={zero_j:.2f}, J({best})={scores[best][0]:.2f} "
        f"over {forecast.horizon_s:g} s ({band.value}); cap {cap_rps} rps"
    )
    return _finish(
        ctx,
        config,
        ranked,
        cap_rps,
        cap_binding,
        best,
        forecast,
        evaluations,
        eligible_now,
        result,
        detail,
        band,
    )


def _finish(
    ctx,
    config,
    ranked,
    cap_rps,
    cap_binding,
    budget,
    forecast,
    evaluations,
    eligible_now,
    result,
    detail,
    band=Band.PESSIMISTIC,
):
    predicted = forecast.budget(budget).band(band) if forecast is not None else None
    horizon = forecast.horizon_s if forecast is not None else None
    if budget == 0:
        d = _build(
            ctx,
            config,
            DecisionMode.NORMAL,
            DecisionReason.NO_SAFE_BUDGET,
            "no positive budget improves on zero; " + detail,
            0,
            (),
            predicted,
            horizon,
        )
        return result(d, cap_rps, evaluations, forecast, ("zero_selected",))
    limit, binding = _selection_limit(ctx, config, budget, eligible_now)
    selected = select_for_dispatch(ranked, ctx.decision_time, limit, ctx.bank_quota)
    if not selected:
        d = _build(
            ctx,
            config,
            DecisionMode.NORMAL,
            DecisionReason.NO_SAFE_BUDGET,
            f"selection limit 0 ({', '.join(binding)}); " + detail,
            0,
            (),
            predicted,
            horizon,
        )
        return result(d, cap_rps, evaluations, forecast, ("selection_limit_zero", *binding))
    d = _build(
        ctx,
        config,
        DecisionMode.NORMAL,
        DecisionReason.SAFE_BUDGET_SELECTED,
        detail,
        budget,
        selected,
        predicted,
        horizon,
    )
    return result(d, cap_rps, evaluations, forecast, ("budget_selected", *cap_binding, *binding))
