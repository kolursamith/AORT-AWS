"""Mappings between Track B's internal models and the shared team contract (``contracts/v1``).

``contracts/v1`` is owned jointly by both tracks; Track B only *maps* to it here and never imports
another owner's internal package. Mapping rules (financial meaning preserved, never invented):

* financial outcome: SUCCESS -> credited; FAILURE / REVERSED -> not_credited; anything else -> unknown;
* workflow: DEBITED/ACTIVE -> initiated, AMBIGUOUS -> ambiguous, CHECK_IN_FLIGHT -> check_pending,
  CLOSED / RESOLVED_BY_RECONCILIATION -> resolved, HANDOFF_UDIR / QUARANTINED -> escalated;
* a status result is ``authoritative`` only for a SUCCESS / FAILED answer obtained from the switch's
  own record or a beneficiary claim (validated backend evidence, SYSTEM_DESIGN invariant 2);
  PENDING / NOT_FOUND map to ``unknown`` with ``authoritative=false``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from contracts.v1.models import (
    EventType,
    FinancialOutcome,
    StatusCheckResult,
    StatusOutcome,
    TransactionEvent,
    WorkflowState,
)

OUTCOME = {
    "SUCCESS": FinancialOutcome.CREDITED,
    "FAILURE": FinancialOutcome.NOT_CREDITED,
    "REVERSED": FinancialOutcome.NOT_CREDITED,
}
WORKFLOW = {
    "DEBITED": WorkflowState.INITIATED,
    "AMBIGUOUS": WorkflowState.AMBIGUOUS,
    "CHECK_IN_FLIGHT": WorkflowState.CHECK_PENDING,
    "CONFIRMED_SUCCESS": WorkflowState.RESOLVED,
    "CONFIRMED_FAILED": WorkflowState.RESOLVED,
    "REVERSAL_INITIATED": WorkflowState.RESOLVED,
    "REVERSED": WorkflowState.RESOLVED,
    "CLOSED": WorkflowState.RESOLVED,
    "RESOLVED_BY_RECONCILIATION": WorkflowState.RESOLVED,
    "HANDOFF_UDIR": WorkflowState.ESCALATED,
    "QUARANTINED": WorkflowState.ESCALATED,
}
EVENT = {
    "PAYMENT_DEBITED": EventType.PAYMENT_INITIATED,
    "PAYMENT_AMBIGUOUS": EventType.PAYMENT_TIMEOUT,
    "CHECK_STARTED": EventType.STATUS_REQUESTED,
    "CHECK_RESULT": EventType.STATUS_RESOLVED,
    "PAYMENT_SUCCEEDED": EventType.STATUS_RESOLVED,
    "PAYMENT_FAILED": EventType.STATUS_RESOLVED,
    "REVERSAL_COMPLETED": EventType.STATUS_RESOLVED,
    "PAYMENT_CLOSED": EventType.STATUS_RESOLVED,
    "ESCALATED": EventType.ESCALATED,
    "QUARANTINED": EventType.ESCALATED,
    "STATUS_RESPONSE": EventType.STATUS_RESOLVED,
}
STATUS = {"SUCCESS": StatusOutcome.CREDITED, "FAILED": StatusOutcome.NOT_CREDITED}


def _aware(ts) -> datetime:
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    if ts is None:
        return datetime.now(UTC)
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def transaction_view_to_event(
    txn, *, experiment_id: str, event_type: EventType | None = None
) -> TransactionEvent:
    """A Track B ``Transaction`` (ORM row or TransactionView) as a contract ``TransactionEvent``."""
    state = str(txn.state)
    return TransactionEvent(
        event_id=f"evt-{txn.transaction_id}-v{txn.version}",
        experiment_id=experiment_id,
        transaction_id=txn.transaction_id,
        bank_id=txn.remitter_bank_id,
        route_id=txn.route_id,
        amount_minor=int(txn.amount_paise),
        currency=txn.currency,
        event_type=event_type or _event_type_for_state(state),
        financial_outcome=OUTCOME.get(str(txn.financial_outcome), FinancialOutcome.UNKNOWN),
        workflow_state=WORKFLOW.get(state, WorkflowState.INITIATED),
        version=int(txn.version),
        occurred_at=_aware(txn.updated_at),
    )


def _event_type_for_state(state: str) -> EventType:
    if state == "DEBITED":
        return EventType.PAYMENT_INITIATED
    if state in ("AMBIGUOUS",):
        return EventType.PAYMENT_TIMEOUT
    if state == "CHECK_IN_FLIGHT":
        return EventType.STATUS_REQUESTED
    if state in ("HANDOFF_UDIR", "QUARANTINED"):
        return EventType.ESCALATED
    return EventType.STATUS_RESOLVED


def domain_event_to_contract(payload: dict, *, experiment_id: str) -> TransactionEvent | None:
    """An outbox ``DomainEvent`` payload as a contract ``TransactionEvent`` (None if not representable)."""
    p = payload.get("payload") or {}
    if not payload.get("transaction_id") or not payload.get("amount_paise"):
        return None
    state = str(p.get("last_known_state") or "")
    return TransactionEvent(
        event_id=str(payload["event_id"]),
        experiment_id=experiment_id,
        transaction_id=payload["transaction_id"],
        bank_id=payload.get("bank_id") or "unknown",
        route_id=payload.get("route_id") or "unknown",
        amount_minor=int(payload["amount_paise"]),
        currency="INR",
        event_type=EVENT.get(str(payload.get("event_type")), _event_type_for_state(state)),
        financial_outcome=OUTCOME.get(str(p.get("financial_outcome")), FinancialOutcome.UNKNOWN),
        workflow_state=WORKFLOW.get(state, WorkflowState.INITIATED),
        version=max(1, int(payload.get("transaction_version") or 1)),
        occurred_at=_aware(payload.get("timestamp")),
    )


def status_to_result(
    *,
    transaction_id: str,
    route_id: str,
    check_id: str,
    status: str,
    version: int,
    latency_ms: float,
    evidence_source_id: str | None,
) -> StatusCheckResult:
    outcome = STATUS.get(str(status), StatusOutcome.UNKNOWN)
    authoritative = outcome != StatusOutcome.UNKNOWN and evidence_source_id is not None
    return StatusCheckResult(
        transaction_id=transaction_id,
        route_id=route_id,
        check_id=check_id,
        outcome=outcome,
        authoritative=authoritative,
        response_version=max(1, int(version)),
        latency_ms=round(max(0.0, latency_ms), 3),
        evidence_source_id=evidence_source_id if authoritative else None,
    )
