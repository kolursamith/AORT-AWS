"""HTTP routes for the remitter bank (banks A1, A2, A3 share this service)."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Header, Query, Request
from sqlalchemy import select

from backend.aorts.app.dependencies import (
    ctx_of,
    error_response,
    require_admin,
    require_flag,
    require_roles,
)
from backend.aorts.banking.ledger.invariants import SafetyViolation
from backend.aorts.banking.ledger.models import Transaction, TransactionEvent
from backend.aorts.banking.remitter.service import SIMULATIONS, RemitterService
from backend.aorts.contracts.enums import ErrorCode, TxnState
from backend.aorts.contracts.requests import (
    EligibilityRequest,
    PaymentRequest,
    ReversalRequest,
    TransactionStateEvent,
)
from backend.aorts.contracts.responses import PaymentResponse, TransactionView
from backend.aorts.security.replay_protection import validate_state_event
from backend.aorts.security.request_auth import VerifiedIdentity

router = APIRouter(prefix="/api/v1")
READERS = ("payment_channel", "dispatcher", "controller", "remitter_service")


def svc(request: Request) -> RemitterService:
    return request.app.state.remitter


def view(txn: Transaction) -> TransactionView:
    return TransactionView.model_validate(txn, from_attributes=True)


@router.post("/payments", response_model=PaymentResponse)
def create_payment(
    req: PaymentRequest,
    request: Request,
    ident: VerifiedIdentity = Depends(require_roles("payment_channel")),
    x_aorts_simulate: str | None = Header(default=None),
):
    simulate = None
    if x_aorts_simulate:
        ctx = ctx_of(request)
        if not ctx.settings.enable_fault_controls or x_aorts_simulate not in SIMULATIONS:
            return error_response(ErrorCode.FEATURE_DISABLED, "simulation header not allowed", 403)
        require_admin(request)
        simulate = x_aorts_simulate
    txn, replayed = svc(request).create_payment(req, simulate)
    return PaymentResponse(
        transaction=view(txn),
        replayed=replayed,
        message="idempotent replay: no second debit" if replayed else txn.state,
    )


@router.get(
    "/payments/{transaction_id}",
    response_model=TransactionView,
    dependencies=[Depends(require_roles(*READERS))],
)
def get_payment(transaction_id: str, request: Request):
    txn = svc(request)._reload(transaction_id)
    if txn is None:
        raise SafetyViolation(ErrorCode.TRANSACTION_NOT_FOUND, "transaction not found", 404)
    return view(txn)


@router.get("/payments/{transaction_id}/history", dependencies=[Depends(require_roles(*READERS))])
def history(
    transaction_id: str,
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    after_id: int = Query(0, ge=0),
):
    with ctx_of(request).db.session() as s:
        rows = (
            s.execute(
                select(TransactionEvent)
                .where(
                    TransactionEvent.transaction_id == transaction_id,
                    TransactionEvent.id > after_id,
                )
                .order_by(TransactionEvent.id)
                .limit(limit)
            )
            .scalars()
            .all()
        )
    items = [
        {
            "id": r.id,
            "event_id": r.event_id,
            "event_type": r.event_type,
            "previous_state": r.previous_state,
            "new_state": r.new_state,
            "previous_version": r.previous_version,
            "new_version": r.new_version,
            "actor": r.actor,
            "reason": r.reason,
            "timestamp": r.created_at.isoformat(),
        }
        for r in rows
    ]
    return {"items": items, "next_after_id": items[-1]["id"] if len(items) == limit else None}


@router.post(
    "/payments/{transaction_id}/simulate-timeout",
    dependencies=[Depends(require_flag("enable_fault_controls")), Depends(require_admin)],
)
def simulate_timeout(transaction_id: str, request: Request):
    """For a HELD (DEBITED, never forwarded) payment: the request 'times out' before reaching the switch."""
    return svc(request).ledger.mark_ambiguous(transaction_id, "simulated timeout").model_dump()


@router.post(
    "/payments/{transaction_id}/simulate-lost-response",
    dependencies=[Depends(require_flag("enable_fault_controls")), Depends(require_admin)],
)
def simulate_lost_response(transaction_id: str, request: Request):
    """For a HELD payment: forward it to the switch, then discard the switch's answer."""
    txn = svc(request)._reload(transaction_id)
    if txn is None or txn.state != TxnState.DEBITED:
        raise SafetyViolation(
            ErrorCode.ILLEGAL_TRANSITION, "only a held DEBITED payment can be forwarded", 409
        )
    return svc(request).forward(transaction_id, drop_response=True).model_dump()


@router.post("/payments/{transaction_id}/reversal")
def reversal(
    transaction_id: str,
    body: ReversalRequest,
    request: Request,
    ident: VerifiedIdentity = Depends(
        require_roles("controller", "dispatcher", "remitter_service")
    ),
):
    return (
        svc(request)
        .ledger.reverse(
            transaction_id,
            body.operation_id,
            expected_version=body.expected_version,
            actor=ident.identity,
            reason=body.reason,
        )
        .model_dump()
    )


@router.post("/payments/{transaction_id}/reconcile")
def reconcile(
    transaction_id: str,
    request: Request,
    ident: VerifiedIdentity = Depends(
        require_roles("dispatcher", "controller", "remitter_service")
    ),
):
    """Resolve an escalated (HANDOFF_UDIR) transaction via the switch's reconciliation channel."""
    return svc(request).reconcile(transaction_id, actor=f"RECON:{ident.identity}")


@router.post("/payments/{transaction_id}/release-quarantine", dependencies=[Depends(require_admin)])
def release_quarantine(transaction_id: str, request: Request, body: dict = Body(...)):
    """Manual review outcome for a QUARANTINED transaction (admin only)."""
    reason = str(body.get("reason") or "")[:200]
    if len(reason) < 3:
        raise SafetyViolation(ErrorCode.VALIDATION_ERROR, "a review reason is required", 422)
    return (
        svc(request)
        .ledger.release_quarantine(
            transaction_id,
            reason,
            actor="ADMIN",
            expected_version=body.get("expected_version"),
            escalate=bool(body.get("escalate")),
        )
        .model_dump()
    )


@router.post("/events/transaction-state")
def transaction_state_event(
    event: TransactionStateEvent,
    request: Request,
    ident: VerifiedIdentity = Depends(require_roles("switch")),
):
    """Signed status-response events from the switch (the surface attacked in Attack 3)."""
    ctx = ctx_of(request)
    txn = svc(request)._reload(event.transaction_id)
    try:
        evidence = validate_state_event(
            event, txn, signer=ident.identity, max_age_seconds=ctx.settings.max_clock_skew_seconds
        )
    except SafetyViolation as exc:
        svc(request).ledger._audit_rejection(
            ident.identity,
            "STATE_EVENT_REJECTED",
            event.transaction_id,
            exc,
            metadata={"event_id": event.event_id},
        )
        ctx.metrics.inc("replay_rejected", reason=exc.code.value)
        raise
    try:
        result = svc(request).ledger.apply_evidence(
            event.transaction_id,
            evidence,
            actor=ident.identity,
            expected_version=event.transaction_version,
            event_id=event.event_id,
            consumer="remitter-state-events",
        )
    except SafetyViolation as exc:
        ctx.metrics.inc("replay_rejected", reason=exc.code.value)
        raise
    ctx.metrics.inc("state_events_applied", decision=result.decision)
    return result.model_dump()


@router.post("/safety/eligibility")
def eligibility(
    body: EligibilityRequest,
    request: Request,
    ident: VerifiedIdentity = Depends(require_roles("controller", "dispatcher")),
):
    return svc(request).ledger.eligibility(
        body.transaction_id, body.proposed_action, body.expected_version
    )


@router.get("/health")
def health(request: Request):
    return {"status": "ok", "service": "remitter"}


@router.get("/metrics")
def metrics(request: Request):
    return ctx_of(request).metrics.snapshot()
