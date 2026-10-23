"""Track B -> Track A v1.1 producers and delivery (integration branch, Stage 09R).

Every v1.1 field comes from a fact Track B recorded when it happened; nothing is inferred
after the fact or asserted by a caller. The Stage 03 adapters (``contracts.v1_1.adapters``)
build the v1.1 messages, so their refusal rules (evidence required, timeouts cannot imply an
outcome, overloaded event types need their recorded disambiguation) apply unchanged.

Producers
---------
* Telemetry (``WindowProducer``): per route, one window ``[start, end)`` closed on the database
  clock. Counts come from the ledger's own append-only ``transaction_events`` (sends, responses
  by recorded cause, check releases and answers), latency from the ``evidence_records`` of the
  responses received in the window, and own outstanding requests from the live row states at
  close. Track B's ``TelemetryValidator`` assesses the window against the switch's independent
  per-destination measurements; REJECTED windows are stored but never delivered, SUSPICIOUS
  ones are delivered with structured flags (the Route Twin keeps them apart).
* Transaction events (``export_event``): outbox ``DomainEvent`` rows plus the provenance
  recorded at the source (``check_id``, ``check_fact``, ``ambiguity_cause``, ``evidence_id``).
  Evidence references come only from verified ``evidence_records``.
* Status-check results (``export_status_result``): the evidence record of an answered check
  with its signature, verification record and the counted attempts after that check.

Delivery (``Delivery``): at-least-once per stream (``events``, ``telemetry``) with a persisted
cursor, bounded exponential backoff on transient failures (5xx, transport), and a dead-letter
row for permanently refused or unrepresentable items. The controller's intake is idempotent,
so redelivery after a crash is harmless. No exactly-once network delivery is claimed.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select, text

from backend.aorts.banking.ledger.models import (
    EvidenceRecord,
    OutboxEvent,
    TelemetryWindow,
    Transaction,
    V11DeadLetter,
    V11DeliveryState,
)
from backend.aorts.contracts.enums import TelemetryVerdict
from backend.aorts.contracts.requests import RouteTelemetry as TrackBTelemetry
from backend.aorts.security.telemetry_validator import TelemetryValidator
from contracts.v1_1 import ErrorCode, SourceSequenceMark, TimingPolicy
from contracts.v1_1.adapters import (
    TRACKB_REJECTION_CODES,
    Adapted,
    Incompatible,
    adapt_outbox_event,
    adapt_status_check,
    adapt_validated_telemetry,
    express_trackb_timing_policy,
)
from contracts.v1_1.protocol import PATH_PREFIX, encode

LEDGER_SOURCE = "trackb-ledger"
VALIDATOR_VERSION = "trackb-validator.1"
# Recorded causes for which the remitter received no usable original response.
NO_RESPONSE_CAUSES = frozenset(
    {
        "TIMEOUT",
        "LOST_RESPONSE",
        "UNREACHABLE",
        "HTTP_ERROR",
        "UNVERIFIED_RESPONSE",
        "WRONG_TRANSACTION",
    }
)
RESPONSE_CAUSES = frozenset({"PENDING_RESPONSE", "NOT_FOUND_RESPONSE"})


def telemetry_source(route_id: str) -> str:
    return f"trackb-remitter-{route_id}"


def trackb_timing_policy(settings, policy_id: str = "trackb-dispatcher-policy") -> TimingPolicy:
    """The timing policy Track B's dispatcher enforces, expressed in v1.1 (Stage 03 adapter)."""

    result = express_trackb_timing_policy(settings.model_dump(), policy_id=policy_id)
    if isinstance(result, Incompatible):
        raise ValueError(f"Track B timing settings are not expressible in v1.1: {result.reasons}")
    return result.value


# ----------------------------------------------------------------------- evidence


def _evidence_reference(rec: EvidenceRecord) -> dict:
    return {
        "evidence_id": rec.evidence_id,
        "evidence_kind": rec.evidence_kind,
        "issuer_id": rec.issuer_id,
        # the issuer's response timestamp (integer seconds) orders its responses
        "response_version": int(rec.response_timestamp or 0),
    }


def _evidence_signature(rec: EvidenceRecord) -> dict:
    return {
        "signature_alg": rec.signature_alg,
        "key_id": rec.key_id,
        "signature": rec.signature_hex,
        "signed_fields": list(rec.signed_fields),
    }


def _verification(rec: EvidenceRecord) -> dict:
    return {
        "status": "VERIFIED" if rec.verified else "FAILED",
        "verifier_id": rec.verifier_id,
        "verified_at": rec.verified_at,
        "key_id": rec.key_id,
    }


def verified_evidence(session, evidence_id: str | None) -> EvidenceRecord | None:
    if not evidence_id:
        return None
    rec = session.get(EvidenceRecord, evidence_id)
    return rec if rec is not None and rec.verified else None


# ----------------------------------------------------------------------- events


def _check_fact_boundary(fact: dict | None) -> dict | None:
    if not fact:
        return None
    kind = fact.get("kind")
    if kind == "REJECTED":
        code = TRACKB_REJECTION_CODES.get(str(fact.get("trackb_code")))
        return {"kind": "REJECTED", "result_code": code} if code else {"kind": "REJECTED"}
    if kind == "TIMED_OUT":
        return {"kind": "TIMED_OUT", "result_code": fact.get("result_code")}
    if kind == "ANSWERED":
        return {"kind": "ANSWERED", "check_result": fact.get("check_result")}
    return None


def export_event(session, row: OutboxEvent, *, experiment_id: str, policy_id: str):
    """One outbox row -> Adapted[v1.1 TransactionEvent] or Incompatible (never guessed)."""

    event = dict(row.payload)
    payload = dict(event.get("payload") or {})
    txn = session.get(Transaction, event.get("transaction_id"))
    if txn is None:
        return Incompatible(ErrorCode.UNKNOWN_TRANSACTION, (), ("no transaction row",))
    if txn.experiment_id != experiment_id:
        return Incompatible(
            ErrorCode.EXPERIMENT_MISMATCH, (), (f"transaction experiment {txn.experiment_id!r}",)
        )
    fact = payload.get("check_fact")
    check_id = payload.get("check_id") or (fact or {}).get("check_id")
    evidence_id = payload.get("evidence_id") or (fact or {}).get("evidence_id")
    evidence = verified_evidence(session, evidence_id)
    boundary = {
        "experiment_id": experiment_id,
        "source_id": LEDGER_SOURCE,
        "source_sequence": row.id,  # BIGSERIAL: strictly increasing in commit order per DB
        "beneficiary_bank_id": txn.beneficiary_bank_id,
        # the payment's own idempotency key is its stable correlation identity
        "correlation_id": event.get("correlation_id") or txn.idempotency_key,
        "deadline_policy_id": policy_id,
    }
    if check_id:
        boundary["check_id"] = check_id
    cause = payload.get("ambiguity_cause")
    if cause in NO_RESPONSE_CAUSES:
        boundary["ambiguity_cause"] = "PAYMENT_TIMEOUT"
    elif cause == "PENDING_RESPONSE":
        boundary["ambiguity_cause"] = "PENDING_RESPONSE"
    if (mapped := _check_fact_boundary(fact)) is not None:
        boundary["check_fact"] = mapped
    if evidence is not None:
        boundary["evidence"] = _evidence_reference(evidence)
    return adapt_outbox_event(event, boundary)


def export_status_result(session, row: OutboxEvent, *, experiment_id: str):
    """The StatusCheckResult carried by an outbox row that records an answered check, or None."""

    event = dict(row.payload)
    payload = dict(event.get("payload") or {})
    fact = payload.get("check_fact") or {}
    check_id = payload.get("check_id") or fact.get("check_id")
    evidence_id = payload.get("evidence_id") or fact.get("evidence_id")
    if not check_id or not evidence_id:
        return None
    rec = session.get(EvidenceRecord, evidence_id)
    if rec is None or rec.evidence_kind != "STATUS_CHECK_RESPONSE":
        return None
    txn = session.get(Transaction, rec.transaction_id)
    if txn is None or txn.experiment_id != experiment_id:
        return None
    outcome = {"admitted": True, "status": rec.reported_status, "latency_ms": rec.latency_ms}
    evidence = {
        "transaction_id": rec.reported_transaction_id,
        "status": rec.reported_status,
        "authenticated": rec.verified,
        "observed_at": rec.verified_at,
    }
    boundary = {
        "experiment_id": experiment_id,
        "check_id": check_id,
        "route_id": txn.route_id,
        "counted_check_attempts": int(payload.get("checks_used") or 0),
        "correlation_id": event.get("correlation_id") or txn.idempotency_key,
    }
    if rec.verified:
        boundary["evidence_reference"] = _evidence_reference(rec)
        boundary["evidence_signature"] = _evidence_signature(rec)
        boundary["verification"] = _verification(rec)
    return adapt_status_check(outcome, evidence, boundary)


# ----------------------------------------------------------------------- telemetry


@dataclass(frozen=True)
class WindowMeasurement:
    route_id: str
    window_start: datetime
    window_end: datetime
    sends: int
    check_releases: int
    payment_responses: int
    payment_timeouts: int
    check_answers: int
    check_timeouts: int
    latencies_ms: tuple[float, ...]
    outstanding: int | None

    @property
    def duration_s(self) -> float:
        return (self.window_end - self.window_start).total_seconds()

    def as_dict(self) -> dict:
        return {
            "sends": self.sends,
            "check_releases": self.check_releases,
            "payment_responses": self.payment_responses,
            "payment_timeouts": self.payment_timeouts,
            "check_answers": self.check_answers,
            "check_timeouts": self.check_timeouts,
            "latency_samples": len(self.latencies_ms),
            "outstanding": self.outstanding,
        }


def _pct(values: tuple[float, ...], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(1, math.ceil(q * len(ordered))) - 1]


def measure_window(
    session, *, experiment_id: str, route_id: str, start: datetime, end: datetime, live: bool
) -> WindowMeasurement:
    """Count the remitter's own traffic on one route in ``[start, end)`` from its ledger."""

    rows = session.execute(
        text(
            """
            SELECT e.event_type, e.previous_state, e.new_state, e.metadata
            FROM transaction_events e JOIN transactions t ON t.transaction_id = e.transaction_id
            WHERE t.route_id = :route AND t.experiment_id = :exp
              AND e.created_at >= :start AND e.created_at < :end
            """
        ),
        {"route": route_id, "exp": experiment_id, "start": start, "end": end},
    ).all()
    sends = releases = responses = timeouts = answers = check_timeouts = 0
    for event_type, prev, _new, meta in rows:
        meta = meta or {}
        fact = meta.get("check_fact") or {}
        if event_type == "PAYMENT_DEBITED":
            sends += 1
        elif event_type == "CHECK_STARTED":
            releases += 1
        elif prev == "DEBITED" and event_type in ("PAYMENT_SUCCEEDED", "PAYMENT_FAILED"):
            responses += 1
        elif prev == "DEBITED" and event_type == "PAYMENT_AMBIGUOUS":
            cause = meta.get("ambiguity_cause")
            if cause in RESPONSE_CAUSES:
                responses += 1
            else:
                timeouts += 1  # recorded no-response cause (or a pre-0002 row without one)
        elif prev == "CHECK_IN_FLIGHT":
            if event_type in ("PAYMENT_SUCCEEDED", "PAYMENT_FAILED") or fact.get("kind") == (
                "ANSWERED"
            ):
                answers += 1
            elif fact.get("kind") == "TIMED_OUT":
                check_timeouts += 1
    latencies = tuple(
        float(v)
        for (v,) in session.execute(
            select(EvidenceRecord.latency_ms)
            .join(Transaction, Transaction.transaction_id == EvidenceRecord.transaction_id)
            .where(
                Transaction.route_id == route_id,
                Transaction.experiment_id == experiment_id,
                EvidenceRecord.created_at >= start,
                EvidenceRecord.created_at < end,
                EvidenceRecord.latency_ms.is_not(None),
                # payment-path latency: what the switch's route measurement also observes
                EvidenceRecord.evidence_kind == "ORIGINAL_PAYMENT_RESPONSE",
            )
        ).all()
    )
    outstanding = None
    if live:  # own requests awaiting an answer right now (payments in flight, checks in flight)
        outstanding = session.execute(
            select(func.count()).where(
                Transaction.route_id == route_id,
                Transaction.experiment_id == experiment_id,
                Transaction.state.in_(["DEBITED", "CHECK_IN_FLIGHT"]),
            )
        ).scalar()
    return WindowMeasurement(
        route_id,
        start,
        end,
        sends,
        releases,
        responses,
        timeouts,
        answers,
        check_timeouts,
        latencies,
        outstanding,
    )


def _flags(assessment) -> tuple[list[str], str]:
    """Validator verdict -> v1.1 flags and cross-check status.

    INCONSISTENT / CROSS_CHECK_DEVIATION mean an unsafe-direction (optimistic) deviation from
    the switch's measurement, or any deviation on a SUSPICIOUS sample. A TRUSTED sample carries
    no flag; it is CONSISTENT when it was cross-checked, UNAVAILABLE otherwise. The validator's
    full reasons stay in the stored window.
    """

    reasons = " ".join(assessment.reasons)
    cross_checked = bool(assessment.measured) and (assessment.measured.get("samples") or 0) > 0
    if assessment.verdict == TelemetryVerdict.TRUSTED:
        return [], "CONSISTENT" if cross_checked and "no independent" not in reasons else (
            "UNAVAILABLE"
        )
    flags = []
    deviation = "unsafe direction" in reasons or "vs measured" in reasons or "bias" in reasons
    if deviation and cross_checked:
        flags.append("CROSS_CHECK_DEVIATION")
    if "anomalous" in reasons or not flags:
        flags.append("STATISTICAL_ANOMALY")
    cross = (
        "INCONSISTENT"
        if "CROSS_CHECK_DEVIATION" in flags
        else ("CONSISTENT" if cross_checked and "no independent" not in reasons else "UNAVAILABLE")
    )
    return flags, cross


class WindowProducer:
    """Closes telemetry windows per route and validates them with Track B's validator."""

    def __init__(
        self,
        db,
        *,
        experiment_id: str,
        routes,
        validator: TelemetryValidator | None = None,
        measured=None,
        start: datetime | None = None,
    ):
        self.db = db
        self.experiment_id = experiment_id
        self.routes = tuple(routes)
        self.validator = validator or TelemetryValidator(
            sources={telemetry_source(r) for r in self.routes}
        )
        self.measured = measured or (lambda: {})
        self.start = start

    def db_now(self) -> datetime:
        with self.db.session() as s:
            return s.execute(text("SELECT now()")).scalar()

    def close(self, route_id: str, end: datetime | None = None, *, report_override=None):
        """Measure, validate and persist the next window of ``route_id``; returns the row.

        ``report_override`` (a dict, or a function of the honest report returning one) lets a
        test or an attack harness replace the reported values to model a compromised source;
        the stored measurement keeps the honest counts.
        """

        source = telemetry_source(route_id)
        live = end is None
        with self.db.session() as s:
            end = end or s.execute(text("SELECT now()")).scalar()
            last = s.execute(
                select(TelemetryWindow)
                .where(
                    TelemetryWindow.experiment_id == self.experiment_id,
                    TelemetryWindow.source_id == source,
                )
                .order_by(TelemetryWindow.sequence.desc())
                .limit(1)
                .with_for_update()
            ).scalar()
            start = last.window_end if last else (self.start or end - timedelta(seconds=1))
            if end <= start:
                return None
            sequence = (last.sequence + 1) if last else 0
            m = measure_window(
                s,
                experiment_id=self.experiment_id,
                route_id=route_id,
                start=start,
                end=end,
                live=live,
            )
            d = m.duration_s
            # The report the validator cross-checks is the payment path only: the switch's
            # independent measurement counts payments, so the remitter's own status checks must
            # not inflate it (they are added back, as a stated split, in the v1.1 export).
            determined = m.payment_responses + m.payment_timeouts
            reported = {
                "event_id": f"tw-{self.experiment_id}-{source}-{sequence}"[:80],
                "source_id": source,
                "route_id": route_id,
                "sequence": sequence,
                "timestamp": end,
                "latency_p50_ms": _pct(m.latencies_ms, 0.5),
                "latency_p95_ms": _pct(m.latencies_ms, 0.95),
                "throughput_tps": m.payment_responses / d,
                "timeout_rate": m.payment_timeouts / determined if determined else None,
                "backlog": m.outstanding,
            }
            if callable(report_override):  # a lie computed from the honest report
                reported.update(report_override(dict(reported)))
            elif report_override:
                reported.update(report_override)
            obs = TrackBTelemetry(
                **{k: v for k, v in reported.items() if k in TrackBTelemetry.model_fields}
            )
            measured = self.measured().get(route_id.split("-", 1)[1])
            assessment = self.validator.validate(
                obs, authenticated_source=source, measured=measured, now=end
            )
            flags, cross = _flags(assessment)
            validated_at = s.execute(text("SELECT now()")).scalar()
            row = TelemetryWindow(
                experiment_id=self.experiment_id,
                source_id=source,
                route_id=route_id,
                sequence=sequence,
                window_start=start,
                window_end=end,
                observed_at=end,
                measurement={
                    **m.as_dict(),
                    "reported": {k: v for k, v in reported.items() if k != "timestamp"},
                    "flags": flags,
                },
                verdict=assessment.verdict.value,
                confidence=assessment.confidence,
                reasons=assessment.reasons,
                cross_check=cross,
                validator_id="trackb-telemetry-validator",
                validator_version=VALIDATOR_VERSION,
                validated_at=max(validated_at, end),
            )
            s.add(row)
            s.flush()
            s.expunge(row)
            return row


def export_window(row: TelemetryWindow):
    """A stored window -> Adapted[ValidatedRouteTelemetry] or Incompatible (Stage 03 adapter)."""

    m = row.measurement
    rep = m["reported"]
    d = (row.window_end - row.window_start).total_seconds()
    observation = {
        "event_id": rep["event_id"],
        "source_id": row.source_id,
        "route_id": row.route_id,
        "sequence": row.sequence,
        "timestamp": row.observed_at,
        "latency_p50_ms": rep.get("latency_p50_ms"),
        "latency_p95_ms": rep.get("latency_p95_ms"),
        # total completions on the route: reported payment completions + own check answers
        "throughput_tps": None
        if rep.get("throughput_tps") is None
        else rep["throughput_tps"] + m["check_answers"] / d,
        "timeout_rate": rep.get("timeout_rate"),
        "backlog": rep.get("backlog"),
    }
    assessment = {
        "event_id": rep["event_id"],
        "route_id": row.route_id,
        "verdict": row.verdict,
        "reasons": row.reasons,
    }
    determined = m["payment_responses"] + m["payment_timeouts"]  # payment path, as reported
    timeouts = m["payment_timeouts"]
    honest = rep.get("throughput_tps") == m["payment_responses"] / d
    boundary = {
        "experiment_id": row.experiment_id,
        "observation_scope": "REMITTER_OWN_TRAFFIC",
        "observation_window_start": row.window_start,
        "observation_window_end": row.window_end,
        "payment_rps": m["sends"] / d,
        "check_rps": m["check_releases"] / d,
        "latency_sample_count": m["latency_samples"]
        if rep.get("latency_p50_ms") is not None
        else 0,
        "trust_status": "AUTHENTICATED",
        "cross_check_status": row.cross_check,
        "validator_id": row.validator_id,
        "validator_version": row.validator_version,
        "validated_at": row.validated_at,
        "validation_flags": list(m.get("flags") or []),
    }
    if honest:  # the rate split is only stated when the reported total is the measured one
        boundary["completed_payment_rps"] = m["payment_responses"] / d
        boundary["completed_check_rps"] = m["check_answers"] / d
    if rep.get("timeout_rate") is not None and timeouts <= determined and determined:
        boundary["timeout_count"] = timeouts
        boundary["timeout_denominator_count"] = determined
    return adapt_validated_telemetry(observation, assessment, boundary)


# ----------------------------------------------------------------------- delivery


@dataclass
class DeliveryReport:
    delivered: int = 0
    duplicates: int = 0
    dead_lettered: int = 0
    retry_pending: bool = False
    status_results: int = 0
    last_error: str | None = None


class Delivery:
    """At-least-once delivery of v1.1 messages to the Track A controller."""

    def __init__(
        self,
        db,
        client: httpx.Client,
        *,
        token: str,
        experiment_id: str,
        policy_id: str,
        batch: int = 500,
        max_backoff_s: float = 60.0,
    ):
        self.db = db
        self.client = client
        self.token = token
        self.experiment_id = experiment_id
        self.policy_id = policy_id
        self.batch = batch
        self.max_backoff_s = max_backoff_s

    @property
    def cursor_stream(self) -> str:
        """The delivery cursor of this experiment's outbox stream (one cursor per experiment:
        a shared cursor let one experiment's delivery skip past another's rows)."""

        key = f"events:{self.experiment_id}"
        if len(key) <= 40:
            return key
        return "events:" + hashlib.sha256(self.experiment_id.encode()).hexdigest()[:33]

    def replay_events(self) -> None:
        """Operator procedure after a controller restart: re-send the outbox from the start.

        Intake is idempotent (duplicates are acknowledged with 200), so the restarted
        controller rebuilds its transaction twins without any second financial effect.
        """

        with self.db.session() as s:
            state = s.get(V11DeliveryState, self.cursor_stream)
            if state is not None:
                state.last_id, state.attempts, state.next_attempt_at = 0, 0, None

    def _post(self, path: str, message) -> httpx.Response:
        return self.client.post(
            f"{PATH_PREFIX}/{path}",
            content=encode(message),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "X-Correlation-ID": getattr(message, "correlation_id", None) or "trackb",
            },
        )

    @staticmethod
    def _classify(resp: httpx.Response) -> str:
        if resp.status_code in (200, 204):
            return "duplicate" if resp.status_code == 200 else "delivered"
        if resp.status_code >= 500 or resp.status_code == 429:
            return "retry"
        return "refused"

    def _state(self, s, stream: str) -> V11DeliveryState:
        st = s.get(V11DeliveryState, stream, with_for_update=True)
        if st is None:
            st = V11DeliveryState(stream=stream, last_id=0, attempts=0)
            s.add(st)
            s.flush()
        return st

    def _dead_letter(self, s, stream, ref_id, code, detail, payload) -> None:
        exists = s.execute(
            select(V11DeadLetter.id).where(
                V11DeadLetter.stream == stream, V11DeadLetter.ref_id == ref_id
            )
        ).first()
        if exists is None:
            s.add(
                V11DeadLetter(
                    stream=stream,
                    ref_id=ref_id,
                    error_code=str(code),
                    detail=detail[:2000],
                    payload=payload,
                )
            )

    def _backoff(self, st: V11DeliveryState, error: str) -> None:
        st.attempts += 1
        st.last_error = error[:1000]
        st.next_attempt_at = datetime.now(UTC) + timedelta(
            seconds=min(self.max_backoff_s, 2 ** min(st.attempts, 10) * 0.1)
        )

    def deliver_events(self, now: datetime | None = None) -> DeliveryReport:
        report = DeliveryReport()
        with self.db.session() as s:
            st = self._state(s, self.cursor_stream)
            if st.next_attempt_at and (now or datetime.now(UTC)) < st.next_attempt_at:
                report.retry_pending = True
                return report
            rows = (
                s.execute(
                    select(OutboxEvent)
                    .where(OutboxEvent.id > st.last_id)
                    .order_by(OutboxEvent.id)
                    .limit(self.batch)
                )
                .scalars()
                .all()
            )
            for row in rows:
                adapted = export_event(
                    s, row, experiment_id=self.experiment_id, policy_id=self.policy_id
                )
                if isinstance(adapted, Incompatible):
                    if (
                        adapted.error_code is not ErrorCode.EXPERIMENT_MISMATCH
                    ):  # other experiments are not ours
                        self._dead_letter(
                            s,
                            "events",
                            row.event_id,
                            adapted.error_code,
                            "; ".join(adapted.reasons) or "not representable",
                            {"missing": list(adapted.missing_fields)},
                        )
                        report.dead_lettered += 1
                    st.last_id = row.id
                    continue
                result = export_status_result(s, row, experiment_id=self.experiment_id)
                try:
                    if isinstance(result, Adapted) and type(result.value).__name__ == (
                        "StatusCheckResult"
                    ):
                        r = self._post("status-check-results", result.value)
                        if self._classify(r) == "retry":
                            raise httpx.HTTPStatusError("retry", request=r.request, response=r)
                        report.status_results += 1
                    resp = self._post("transaction-events", adapted.value)
                except httpx.HTTPError as exc:
                    self._backoff(st, f"{type(exc).__name__}: {exc}")
                    report.retry_pending, report.last_error = True, st.last_error
                    return report
                outcome = self._classify(resp)
                if outcome == "retry":
                    self._backoff(st, f"HTTP {resp.status_code}")
                    report.retry_pending, report.last_error = True, st.last_error
                    return report
                if outcome == "refused":
                    body = resp.json() if resp.content else {}
                    self._dead_letter(
                        s,
                        "events",
                        row.event_id,
                        body.get("error_code", "REFUSED"),
                        body.get("message", f"HTTP {resp.status_code}"),
                        adapted.value.model_dump(mode="json"),
                    )
                    report.dead_lettered += 1
                else:
                    report.delivered += outcome == "delivered"
                    report.duplicates += outcome == "duplicate"
                st.last_id, st.attempts, st.next_attempt_at, st.last_error = row.id, 0, None, None
        return report

    def deliver_telemetry(self) -> DeliveryReport:
        report = DeliveryReport()
        with self.db.session() as s:
            rows = (
                s.execute(
                    select(TelemetryWindow)
                    .where(
                        TelemetryWindow.experiment_id == self.experiment_id,
                        TelemetryWindow.delivered_at.is_(None),
                        TelemetryWindow.verdict != TelemetryVerdict.REJECTED.value,
                    )
                    .order_by(TelemetryWindow.id)
                    .limit(self.batch)
                    .with_for_update(skip_locked=True)
                )
                .scalars()
                .all()
            )
            for row in rows:
                adapted = export_window(row)
                if isinstance(adapted, Incompatible):
                    self._dead_letter(
                        s,
                        "telemetry",
                        f"{row.source_id}:{row.sequence}",
                        adapted.error_code,
                        "; ".join(adapted.reasons),
                        {"missing": list(adapted.missing_fields)},
                    )
                    row.delivered_at = datetime.now(UTC)
                    row.last_error = f"not representable: {adapted.error_code}"
                    report.dead_lettered += 1
                    continue
                try:
                    resp = self._post("telemetry", adapted.value)
                except httpx.HTTPError as exc:
                    row.attempts += 1
                    row.last_error = f"{type(exc).__name__}"
                    report.retry_pending, report.last_error = True, row.last_error
                    return report
                outcome = self._classify(resp)
                if outcome == "retry":
                    row.attempts += 1
                    row.last_error = f"HTTP {resp.status_code}"
                    report.retry_pending, report.last_error = True, row.last_error
                    return report
                if outcome == "refused":
                    body = resp.json() if resp.content else {}
                    self._dead_letter(
                        s,
                        "telemetry",
                        f"{row.source_id}:{row.sequence}",
                        body.get("error_code", "REFUSED"),
                        body.get("message", ""),
                        adapted.value.model_dump(mode="json"),
                    )
                    row.last_error = f"refused: {body.get('error_code', 'REFUSED')}"
                    report.dead_lettered += 1
                else:
                    report.delivered += outcome == "delivered"
                    report.duplicates += outcome == "duplicate"
                    row.last_error = None  # acknowledged by the controller
                row.delivered_at = datetime.now(UTC)
        return report


def delivered_watermark(db, experiment_id: str, route_id: str) -> tuple[SourceSequenceMark, ...]:
    """Highest telemetry sequence the controller acknowledged for ``route_id`` (204 or 200).

    Sent as ``min_telemetry_watermark`` so a controller that has fallen behind what Track B
    knows it delivered (a restart, a lost update) fails closed instead of deciding on older
    state. Refused or not-yet-delivered windows never raise the watermark.
    """

    source = telemetry_source(route_id)
    with db.session() as s:
        seq = s.execute(
            select(func.max(TelemetryWindow.sequence)).where(
                TelemetryWindow.experiment_id == experiment_id,
                TelemetryWindow.source_id == source,
                TelemetryWindow.delivered_at.is_not(None),
                TelemetryWindow.last_error.is_(None),
            )
        ).scalar()
    return () if seq is None else (SourceSequenceMark(source_id=source, sequence=seq),)
