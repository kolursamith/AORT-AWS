"""AORTS v1.1 recovery controller core (Stage 09): twins -> estimator -> look-ahead -> optimizer.

``ControllerCore`` is the transport-independent heart of the controller service. It owns, for
one experiment, the Track A Route Twin, the Transaction Twin, one Stage 06 capacity estimator
per route and the controller-side sensing state (answered latency, timeout rates, check
outcomes, late resolutions). It never reads the system clock (an injected ``Clock`` supplies
time), never generates random identifiers and never touches money: it ingests facts that
Track B committed and answers decision requests with strict v1.1 ``RecoveryDecision`` values
that Track B's execution gate re-validates.

Intake (each returns an ``IntakeOutcome`` carrying the v1.1 HTTP status and, when not 204, an
``ErrorResponse``):

* telemetry: experiment check, then the Route Twin (registered sources, AUTHENTICATED only,
  flagged samples kept apart, per-source sequences, staleness and future skew). Applied trusted
  windows also update that route's estimator and latency/timeout history.
* transaction events: experiment check, allowed event sources, then the Transaction Twin
  (idempotency by event_id and state version, conflicts quarantine, evidence rules). Applied
  timeouts of checks and late resolutions update sensing state.
* status-check results: sensing data only (the matching TransactionEvent changes the
  projection). Idempotent by ``check_id``; unknown transactions and route mismatches refused.
  An authoritative answer counts as authoritative only when Track B verified its evidence.

Decisions: ``validate_decision_request`` (protocol), a telemetry watermark the controller has
not reached -> FAIL_CLOSED ``TELEMETRY_STALE``; otherwise the Stage 06 estimate at the request
instant, the eligible pool from the Transaction Twin, the Stage 07 forecast and the Stage 08
optimizer. Every decision gets a unique, deterministic ID (sequence number + content) and a
``DecisionRecord`` saying which component produced it (``optimizer``, ``watermark_gate`` or
``internal_error``), so a fallback is never attributed to the optimizer.
"""

import bisect
import hashlib
import threading
import time
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from contracts.v1_1 import (
    SCHEMA_VERSION,
    CheckResultKind,
    DecisionMode,
    DecisionReason,
    DecisionRequest,
    ErrorCode,
    ErrorResponse,
    RecoveryDecision,
    RecoveryWorkflowState,
    RouteDescriptor,
    StatusCheckResult,
    TimingPolicy,
    TransactionEvent,
    TransactionEventType,
    ValidatedRouteTelemetry,
)
from contracts.v1_1.errors import HTTP_STATUS
from contracts.v1_1.protocol import (
    PROTOCOL_VERSION,
    ControllerStatus,
    ProtocolViolation,
    build_fail_closed_decision,
    decode,
    encode,
    error_response,
    lagging_sources,
    timing_policy_reference,
    validate_decision_request,
)
from intelligence.common.clock import Clock
from intelligence.controller.lookahead import CheckEvidence, CheckOutcomeCounts
from intelligence.controller.optimizer import (
    MODEL_VERSION,
    DecisionContext,
    OptimizerConfig,
    RouteObservables,
    decide,
)
from intelligence.controller.ranker import pool_from_twin
from intelligence.ml.capacity_estimator import (
    CapacityEstimate,
    CapacityEstimator,
    CapacityEstimatorConfig,
    RouteState,
)
from intelligence.twin.route_twin import IngestStatus, RouteTwinConfig, RouteTwinRegistry
from intelligence.twin.transaction_twin import (
    ApplyStatus,
    TransactionTwinConfig,
    TransactionTwinRegistry,
    UnknownTransactionError,
)

SERVICE_VERSION = "aorts-controller.1"
# codes whose check counting the dispatcher declares; no check is involved at intake
_DECLARED = frozenset({ErrorCode.INTERNAL_ERROR, ErrorCode.UPSTREAM_UNAVAILABLE})
_AMBIGUOUS_STATES = frozenset(
    {
        RecoveryWorkflowState.AMBIGUOUS,
        RecoveryWorkflowState.CHECK_IN_FLIGHT,
        RecoveryWorkflowState.ESCALATED,
    }
)


class RouteRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    descriptor: RouteDescriptor
    telemetry_sources: tuple[str, ...] = Field(min_length=1)


class ControllerConfig(BaseModel):
    """Everything the controller needs for one experiment. No secret lives here."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    experiment_id: str
    service_id: str = "aorts-controller"
    timing_policy: TimingPolicy
    routes: tuple[RouteRegistration, ...] = Field(min_length=1)
    event_sources: tuple[str, ...] = Field(min_length=1)
    twin: RouteTwinConfig
    estimator: CapacityEstimatorConfig = CapacityEstimatorConfig()
    optimizer: OptimizerConfig = OptimizerConfig()
    max_request_age_s: float = Field(default=5.0, gt=0)
    max_future_skew_s: float = Field(default=1.0, ge=0)
    checks_per_bank_per_decision: int | None = Field(default=10, ge=0)
    history_windows: int = Field(default=10, ge=1)
    outcome_memory: int = Field(default=200, ge=1)
    resolution_window_s: float = Field(default=60.0, gt=0)


@dataclass(frozen=True)
class IntakeOutcome:
    status_code: int
    error: ErrorResponse | None = None
    detail: str = ""

    @property
    def accepted(self) -> bool:
        return self.status_code == 204


@dataclass(frozen=True)
class DecisionRecord:
    """Audit entry for one decision. ``source`` names who produced it."""

    sequence: int
    decision_id: str
    route_id: str
    created_at: datetime
    source: str  # optimizer | watermark_gate | internal_error
    mode: str
    reason: str
    budget_rps: int
    selected: int
    cause: str
    timings_ms: tuple[tuple[str, float], ...]
    response_bytes: int


@dataclass
class _RouteSensing:
    estimator: CapacityEstimator
    latest_estimate: CapacityEstimate | None = None
    latencies_s: deque = field(default_factory=deque)
    timeouts: deque = field(default_factory=deque)  # (timeouts, determined)
    outstanding: int | None = None
    state_times: list = field(default_factory=list)  # (epoch seconds, congested)
    outcomes: deque = field(default_factory=deque)  # CheckResultKind | None
    resolutions: deque = field(default_factory=deque)  # epoch seconds of late resolutions
    last_result: Any = None  # the latest OptimizationResult (read-only diagnostics)
    last_decision: Any = None  # the latest published RecoveryDecision (any source)


def _secs(t: datetime) -> float:
    return t.timestamp()


class ControllerCore:
    def __init__(self, config: ControllerConfig, clock: Clock) -> None:
        self.config = config
        self._clock = clock
        self._lock = threading.Lock()
        self._route_twin = RouteTwinRegistry(config.twin, clock)
        self._txn_twin = TransactionTwinRegistry(
            config.timing_policy, clock, TransactionTwinConfig(require_registered_routes=True)
        )
        self._routes: dict[str, _RouteSensing] = {}
        for reg in config.routes:
            d = reg.descriptor
            if d.experiment_id != config.experiment_id:
                raise ValueError(f"route {d.route_id} belongs to another experiment")
            self._route_twin.register_route(d, reg.telemetry_sources)
            self._txn_twin.register_route(d)
            self._routes[d.route_id] = _RouteSensing(
                CapacityEstimator(config.experiment_id, d.route_id, config.estimator)
            )
        self._event_sources = frozenset(config.event_sources)
        self._check_results: dict[str, str] = {}  # check_id -> fingerprint
        self._evidence: dict[str, CheckEvidence] = {}
        self._sequence = 0
        self.records: deque[DecisionRecord] = deque(maxlen=10_000)
        self.counters: dict[str, int] = {}

    # ------------------------------------------------------------------ helpers

    def _count(self, key: str) -> None:
        self.counters[key] = self.counters.get(key, 0) + 1

    def _error(self, code: ErrorCode, message: str, correlation_id=None) -> IntakeOutcome:
        body = error_response(
            code,
            message,
            experiment_id=self.config.experiment_id,
            correlation_id=correlation_id,
            counts_against_limit=False if code in _DECLARED else None,
        )
        return IntakeOutcome(HTTP_STATUS[code], body, message)

    def _projection(self, transaction_id: str):
        try:
            return self._txn_twin.snapshot(self.config.experiment_id, transaction_id)
        except UnknownTransactionError:
            return None

    @property
    def known_routes(self) -> frozenset[str]:
        return frozenset(self._routes)

    # ------------------------------------------------------------------ intake

    def ingest_telemetry(self, raw, correlation_id: str | None = None) -> IntakeOutcome:
        try:
            item = (
                raw
                if isinstance(raw, ValidatedRouteTelemetry)
                else decode(ValidatedRouteTelemetry, raw)
            )
        except ProtocolViolation as v:
            return self._error(v.code, v.message, correlation_id)
        if item.experiment_id != self.config.experiment_id:
            return self._error(
                ErrorCode.EXPERIMENT_MISMATCH, "telemetry is for another experiment", correlation_id
            )
        with self._lock:
            result = self._route_twin.ingest(item)
            self._count(f"telemetry_{result.status.value.lower()}")
            if result.status is IngestStatus.APPLIED:
                sensing = self._routes[item.route_id]
                now = self._clock.now()
                snapshot = self._route_twin.snapshot(item.experiment_id, item.route_id)
                sensing.latest_estimate = sensing.estimator.update(snapshot, now)
                if result.trusted:
                    self._remember_window(sensing, item, sensing.latest_estimate)
                return IntakeOutcome(204, None, result.detail)
        if result.error_code is None:
            return self._error(ErrorCode.INTERNAL_ERROR, result.detail, correlation_id)
        return self._error(result.error_code, result.detail, correlation_id)

    def _remember_window(self, sensing, item, estimate) -> None:
        cap = self.config.history_windows
        t = item.telemetry
        if t.p50_latency_ms is not None and t.latency_sample_count >= 3:
            sensing.latencies_s.append(t.p50_latency_ms / 1000.0)
            while len(sensing.latencies_s) > cap:
                sensing.latencies_s.popleft()
        if t.timeout_count is not None:
            sensing.timeouts.append((t.timeout_count, t.timeout_denominator_count or 0))
            while len(sensing.timeouts) > cap:
                sensing.timeouts.popleft()
        sensing.outstanding = t.queue_depth
        stamp = _secs(t.observation_window_end)
        congested = estimate.route_state is RouteState.CONGESTED
        if not sensing.state_times or sensing.state_times[-1][0] < stamp:
            sensing.state_times.append((stamp, congested))
            if len(sensing.state_times) > 4 * 3600:
                del sensing.state_times[:3600]

    def ingest_event(self, raw, correlation_id: str | None = None) -> IntakeOutcome:
        try:
            event = raw if isinstance(raw, TransactionEvent) else decode(TransactionEvent, raw)
        except ProtocolViolation as v:
            return self._error(v.code, v.message, correlation_id)
        if event.experiment_id != self.config.experiment_id:
            return self._error(
                ErrorCode.EXPERIMENT_MISMATCH, "event is for another experiment", correlation_id
            )
        if event.source_id not in self._event_sources:
            return self._error(
                ErrorCode.UNKNOWN_SOURCE,
                f"event source {event.source_id!r} is not registered",
                correlation_id,
            )
        if event.route_id not in self._routes:
            return self._error(
                ErrorCode.ROUTE_IDENTITY_MISMATCH, "route is not registered", correlation_id
            )
        with self._lock:
            before = self._projection(event.transaction_id)
            result = self._txn_twin.apply_event(event)
            self._count(f"event_{result.status.value.lower()}")
            if result.status is ApplyStatus.APPLIED:
                self._sense_event(event, before)
                return IntakeOutcome(204, None, result.detail)
        code = result.error_code or ErrorCode.INTERNAL_ERROR
        return self._error(code, result.detail, correlation_id)

    def _sense_event(self, event: TransactionEvent, before) -> None:
        sensing = self._routes[event.route_id]
        kind = event.event_type
        if kind is TransactionEventType.STATUS_CHECK_TIMED_OUT:
            self._evidence[event.transaction_id] = CheckEvidence.LAST_CHECK_TIMED_OUT
            self._remember_outcome(sensing, None)
        was_ambiguous = before is not None and before.workflow_state in _AMBIGUOUS_STATES
        if was_ambiguous and kind in (
            TransactionEventType.PAYMENT_RESPONSE_RECEIVED,
            TransactionEventType.RECONCILIATION_RESOLVED,
        ):
            sensing.resolutions.append(_secs(event.event_timestamp))
            while len(sensing.resolutions) > 100_000:
                sensing.resolutions.popleft()

    def _remember_outcome(self, sensing: _RouteSensing, result: CheckResultKind | None) -> None:
        sensing.outcomes.append(result)
        while len(sensing.outcomes) > self.config.outcome_memory:
            sensing.outcomes.popleft()

    def ingest_status_result(self, raw, correlation_id: str | None = None) -> IntakeOutcome:
        try:
            result = raw if isinstance(raw, StatusCheckResult) else decode(StatusCheckResult, raw)
        except ProtocolViolation as v:
            return self._error(v.code, v.message, correlation_id)
        if result.experiment_id != self.config.experiment_id:
            return self._error(
                ErrorCode.EXPERIMENT_MISMATCH, "result is for another experiment", correlation_id
            )
        fingerprint = hashlib.sha256(encode(result)).hexdigest()
        with self._lock:
            seen = self._check_results.get(result.check_id)
            if seen == fingerprint:
                self._count("status_duplicate")
                return self._error(
                    ErrorCode.DUPLICATE_EVENT, "identical result already received", correlation_id
                )
            if seen is not None:
                self._count("status_conflict")
                return self._error(
                    ErrorCode.DUPLICATE_CHECK_ID,
                    "check_id reused with different content",
                    correlation_id,
                )
            projection = self._projection(result.transaction_id)
            if projection is None:
                return self._error(
                    ErrorCode.UNKNOWN_TRANSACTION, "transaction has no projection", correlation_id
                )
            if projection.route_id != result.route_id or result.route_id not in self._routes:
                return self._error(
                    ErrorCode.ROUTE_IDENTITY_MISMATCH,
                    "result route differs from the transaction's route",
                    correlation_id,
                )
            self._check_results[result.check_id] = fingerprint
            kind = result.check_result
            if (
                kind
                in (CheckResultKind.AUTHORITATIVE_SUCCESS, CheckResultKind.AUTHORITATIVE_FAILURE)
                and not result.is_authoritative
            ):
                kind = CheckResultKind.PENDING  # unverified evidence is not authoritative
            self._remember_outcome(self._routes[result.route_id], kind)
            if kind is CheckResultKind.UNKNOWN:
                self._evidence[result.transaction_id] = CheckEvidence.LAST_CHECK_UNKNOWN
            elif kind is CheckResultKind.PENDING:
                self._evidence[result.transaction_id] = CheckEvidence.LAST_CHECK_PENDING
            else:
                self._evidence.pop(result.transaction_id, None)
            self._count("status_applied")
            return IntakeOutcome(204, None, "recorded")

    # ------------------------------------------------------------------ decisions

    def decide(
        self, raw, correlation_id: str | None = None
    ) -> tuple[RecoveryDecision | None, IntakeOutcome | None, DecisionRecord | None]:
        """Returns (decision, None, record) or (None, refusal, None)."""

        began = time.perf_counter()
        now = self._clock.now()
        try:
            request = validate_decision_request(
                raw,
                experiment_id=self.config.experiment_id,
                known_routes=self.known_routes,
                now=now,
                max_request_age_s=self.config.max_request_age_s,
                max_future_skew_s=self.config.max_future_skew_s,
            )
        except ProtocolViolation as v:
            self._count("decision_refused")
            return None, self._error(v.code, v.message, correlation_id), None
        validated = time.perf_counter()
        with self._lock:
            self._sequence += 1
            seq = self._sequence
            try:
                decision, source, cause, timings = self._decide_locked(request, now)
            except Exception as exc:  # noqa: BLE001 - any internal fault must fail closed
                decision = build_fail_closed_decision(
                    request,
                    decision_id="pending",
                    reason=DecisionReason.NO_CAPACITY_ESTIMATE,
                    detail=f"internal error: {type(exc).__name__}",
                    created_at=now,
                    validity_s=self.config.optimizer.decision_validity_s,
                    policy_version=SERVICE_VERSION,
                    model_version=MODEL_VERSION,
                    timing_policy_id=self.config.timing_policy.policy_id,
                )
                source, cause, timings = "internal_error", f"{type(exc).__name__}: {exc}", ()
            decision = self._with_unique_id(decision, seq)
            self._routes[decision.route_id].last_decision = decision
            body = encode(decision)
            total_ms = (time.perf_counter() - began) * 1000.0
            record = DecisionRecord(
                sequence=seq,
                decision_id=decision.decision_id,
                route_id=decision.route_id,
                created_at=decision.created_at,
                source=source,
                mode=decision.decision_mode.value,
                reason=decision.reason_code.value,
                budget_rps=decision.budget_rps,
                selected=len(decision.selected_transaction_ids),
                cause=cause[:500],
                timings_ms=(
                    ("validation", (validated - began) * 1000.0),
                    *timings,
                    ("total", total_ms),
                ),
                response_bytes=len(body),
            )
            self.records.append(record)
            self._count(f"decision_{source}")
            return decision, None, record

    def _decide_locked(self, request: DecisionRequest, now: datetime):
        exp, route = request.experiment_id, request.route_id
        sensing = self._routes[route]
        t0 = time.perf_counter()
        snapshot = self._route_twin.snapshot(exp, route)
        accepted = {m.source_id: m.sequence for m in snapshot.latest_sequence_per_source}
        lagging = lagging_sources(request.min_telemetry_watermark, accepted)
        if lagging:
            decision = build_fail_closed_decision(
                request,
                decision_id="pending",
                reason=DecisionReason.TELEMETRY_STALE,
                detail=f"controller has not reached the requested watermark for "
                f"{sorted(m.source_id for m in lagging)}",
                created_at=now,
                validity_s=self.config.optimizer.decision_validity_s,
                policy_version=SERVICE_VERSION,
                model_version=MODEL_VERSION,
                timing_policy_id=self.config.timing_policy.policy_id,
                telemetry_watermark=snapshot.latest_sequence_per_source,
                latest_observation_at=snapshot.last_observed_at,
            )
            return decision, "watermark_gate", "lagging watermark", ()
        estimate = sensing.estimator.update(snapshot, now)
        sensing.latest_estimate = estimate
        t1 = time.perf_counter()
        pool = pool_from_twin(
            self._txn_twin,
            exp,
            route,
            now,
            self._evidence,
            lambda sent: self._congested_at(sensing, sent),
        )
        backlog = sum(
            1
            for txn in self._txn_twin.transaction_ids(exp)
            if (p := self._txn_twin.snapshot(exp, txn)).route_id == route
            and p.workflow_state in _AMBIGUOUS_STATES
        )
        t2 = time.perf_counter()
        observables = self._observables(sensing, now, backlog)
        quota = None
        if self.config.checks_per_bank_per_decision is not None:
            banks = {e.candidate.remitter_bank_id or "" for e in pool}
            quota = {b: self.config.checks_per_bank_per_decision for b in banks}
        ctx = DecisionContext(
            experiment_id=exp,
            route_id=route,
            decision_time=now,
            snapshot=snapshot,
            capacity=estimate,
            pool=pool,
            observables=observables,
            policy=self.config.timing_policy,
            bank_quota=quota,
            credit_allowance=None,
        )
        result = decide(ctx, self.config.optimizer)
        sensing.last_result = result
        t3 = time.perf_counter()
        timings = (
            ("twin_and_estimator", (t1 - t0) * 1000.0),
            ("pool", (t2 - t1) * 1000.0),
            ("lookahead_and_optimizer", (t3 - t2) * 1000.0),
        )
        return result.decision, "optimizer", ",".join(result.reasons), timings

    def _congested_at(self, sensing: _RouteSensing, sent: datetime) -> bool | None:
        stamps = sensing.state_times
        if not stamps:
            return None
        i = bisect.bisect_right(stamps, (_secs(sent), True)) - 1
        return None if i < 0 else stamps[i][1]

    def _observables(self, sensing: _RouteSensing, now: datetime, backlog: int):
        lat = sorted(sensing.latencies_s)
        median = lat[len(lat) // 2] if lat else None
        t_count = sum(t for t, _ in sensing.timeouts)
        t_den = sum(d for _, d in sensing.timeouts)
        counts = CheckOutcomeCounts()
        for outcome in sensing.outcomes:
            counts = counts.record(outcome)
        horizon = _secs(now) - self.config.resolution_window_s
        while sensing.resolutions and sensing.resolutions[0] < horizon:
            sensing.resolutions.popleft()
        recent = len(sensing.resolutions)
        hazard = recent / (backlog * self.config.resolution_window_s) if backlog > 0 else None
        return RouteObservables(
            observed_outstanding=sensing.outstanding,
            observed_median_latency_s=median,
            observed_timeout_rate=(t_count / t_den) if t_den else None,
            late_resolution_hazard_per_s=hazard,
            ambiguous_backlog=backlog,
            current_check_rps=0.0,
            check_outcomes=counts,
        )

    def _with_unique_id(self, decision: RecoveryDecision, seq: int) -> RecoveryDecision:
        digest = hashlib.sha256(encode(decision)).hexdigest()[:24]
        data = decision.model_dump()
        data["decision_id"] = f"dec-{seq:08d}-{digest}"
        return RecoveryDecision.model_validate(data)

    # ------------------------------------------------------------------ diagnostics

    def introspection(self, max_projections: int = 200) -> dict:
        """Read-only diagnostics for operators and the dashboard (not a v1.1 contract message).

        Per route: the Route Twin snapshot, the latest Stage 06 estimate and the latest Stage 08
        optimisation (candidate budgets with their objective terms and the Stage 07 forecast
        bands, labelled as model predictions). Plus the newest Transaction Twin projections and
        decision records. Nothing here changes state, and no secret is included.
        """

        import json
        from dataclasses import asdict

        def plain(value):
            return json.loads(json.dumps(value, default=str))

        with self._lock:
            exp = self.config.experiment_id
            routes = {}
            for route, sensing in sorted(self._routes.items()):
                snap = self._route_twin.snapshot(exp, route)
                est = sensing.latest_estimate
                res = sensing.last_result
                optimisation = None
                if res is not None:
                    optimisation = {
                        "kind": "MODEL_PREDICTION",
                        "ablation": res.ablation.value,
                        "profile": res.profile,
                        "cap_rps": res.cap_rps,
                        "selected_budget_rps": res.selected_budget_rps,
                        "reasons": list(res.reasons),
                        "capacity_confidence_heuristic": res.capacity_confidence,
                        # the optimizer's own result; the published id is in "last_decision"
                        "optimizer_decision": plain(res.decision.model_dump(mode="json")),
                        "evaluations": plain([asdict(e) for e in res.evaluations]),
                        "forecast": None if res.forecast is None else plain(asdict(res.forecast)),
                    }
                routes[route] = {
                    "snapshot": plain(snap.model_dump(mode="json")),
                    "estimate": None if est is None else plain(est.model_dump(mode="json")),
                    "optimisation": optimisation,
                    "last_decision": None
                    if sensing.last_decision is None
                    else plain(sensing.last_decision.model_dump(mode="json")),
                }
            snaps = [self._txn_twin.snapshot(exp, t) for t in self._txn_twin.transaction_ids(exp)]
            # payments that became ambiguous first (the recovery story), then the newest others
            ambiguous = [p for p in snaps if p.ambiguity_created_at is not None][-max_projections:]
            others = [p for p in snaps if p.ambiguity_created_at is None][-50:]
            projections = [plain(p.model_dump(mode="json")) for p in ambiguous + others]
            records = [plain(asdict(r)) for r in list(self.records)[-100:]]
            return {
                "experiment_id": exp,
                "generated_at": self._clock.now().isoformat(),
                "routes": routes,
                "transaction_projections": projections,
                "decision_records": records,
                "counters": dict(self.counters),
            }

    # ------------------------------------------------------------------ status

    def status(self) -> ControllerStatus:
        reasons = []
        with self._lock:
            for route, sensing in sorted(self._routes.items()):
                snap = self._route_twin.snapshot(self.config.experiment_id, route)
                if snap.freshness_status.value != "FRESH":
                    reasons.append(f"route {route}: telemetry {snap.freshness_status.value}")
                elif sensing.latest_estimate is None:
                    reasons.append(f"route {route}: no capacity estimate yet")
        return ControllerStatus(
            schema_version=SCHEMA_VERSION,
            protocol_version=PROTOCOL_VERSION,
            service_id=self.config.service_id,
            experiment_id=self.config.experiment_id,
            ready=not reasons,
            policy_version=SERVICE_VERSION,
            model_version=MODEL_VERSION,
            timing_policy=timing_policy_reference(self.config.timing_policy),
            not_ready_reasons=tuple(reasons),
        )

    def decision_log(self) -> tuple[DecisionRecord, ...]:
        return tuple(self.records)


def replay(core: ControllerCore, items: Iterable[tuple[str, Any]]) -> list[IntakeOutcome]:
    """Feed (kind, message) pairs in order; kinds: telemetry, event, status."""

    handlers: Mapping[str, Any] = {
        "telemetry": core.ingest_telemetry,
        "event": core.ingest_event,
        "status": core.ingest_status_result,
    }
    return [handlers[kind](message) for kind, message in items]


def mode_is_zero(decision: RecoveryDecision) -> bool:
    return decision.decision_mode is not DecisionMode.NORMAL or decision.budget_rps == 0
