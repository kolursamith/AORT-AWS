"""Team-contract HTTP surface (``docs/API_CONTRACTS.md``, ``contracts/v1``) served by Track B services.

These routes are thin adapters over Track B's real implementation (authoritative ledger, admission
control, evidence rules). They keep Track B's security model: every call is HMAC-signed by the
caller's service identity, and identities are bound to the claimed bank.

| Service     | Endpoint                         | Contract message            |
|-------------|----------------------------------|-----------------------------|
| remitter    | ``POST /v1/payments``            | -> ``TransactionEvent`` (202) |
| remitter    | ``GET /v1/payments/{id}``        | -> ``TransactionEvent``       |
| switch      | ``POST /v1/status-checks``       | -> ``StatusCheckResult``      |
| beneficiary | ``GET /v1/mock/status/{id}``     | -> ``StatusCheckResult``      |
| control     | ``GET /v1/events``               | -> ``[TransactionEvent]``     |
| control     | ``POST /v1/telemetry``           | ``RouteTelemetry`` -> verdict |
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from backend.aorts.adapters.team_contract import (
    domain_event_to_contract,
    status_to_result,
    transaction_view_to_event,
)
from backend.aorts.app.dependencies import ctx_of, error_response, require_roles
from backend.aorts.banking.ledger.invariants import SafetyViolation
from backend.aorts.banking.ledger.models import OutboxEvent, SwitchTransaction
from backend.aorts.contracts.enums import ErrorCode
from backend.aorts.contracts.requests import PaymentRequest, StatusCheckRequest
from backend.aorts.contracts.requests import RouteTelemetry as TrackBTelemetry
from backend.aorts.security.request_auth import VerifiedIdentity
from contracts.v1.models import RouteTelemetry, StatusCheckResult, TransactionEvent


def _experiment(request: Request) -> str:
    return ctx_of(request).settings.experiment_id


# ---------------------------------------------------------------- remitter
remitter_v1 = APIRouter(prefix="/v1", tags=["team-contract"])


class ContractPaymentRequest(BaseModel):
    """Team-contract payment request; Track B additionally requires an idempotency key."""

    model_config = ConfigDict(extra="forbid")
    bank_id: str = Field(pattern=r"^[A-Z][A-Z0-9]{0,7}$")
    route_id: str = Field(pattern=r"^[A-Z][A-Z0-9]{0,7}-[A-Z][A-Z0-9]{0,7}$")
    amount_minor: int = Field(ge=1, le=10_000_000_00)
    currency: str = Field(pattern=r"^INR$")
    experiment_id: str | None = None
    idempotency_key: str = Field(
        min_length=8, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,79}$"
    )
    sender_account_id: str | None = None
    beneficiary_account_id: str | None = None


@remitter_v1.post("/payments", response_model=TransactionEvent, status_code=202)
def contract_payment(
    body: ContractPaymentRequest,
    request: Request,
    ident: VerifiedIdentity = Depends(require_roles("payment_channel")),
):
    bank, dest = body.route_id.split("-", 1)
    if bank != body.bank_id:
        raise SafetyViolation(ErrorCode.VALIDATION_ERROR, "route_id must start with bank_id", 422)
    req = PaymentRequest(
        sender_account_id=body.sender_account_id or f"{bank}-CUST-0001",
        beneficiary_account_id=body.beneficiary_account_id or f"{dest}-CUST-0001",
        beneficiary_bank_id=dest,
        amount_paise=body.amount_minor,
        currency=body.currency,
        idempotency_key=body.idempotency_key,
        correlation_id=body.experiment_id,
    )
    txn, _ = request.app.state.remitter.create_payment(req)
    return transaction_view_to_event(txn, experiment_id=body.experiment_id or _experiment(request))


@remitter_v1.get(
    "/payments/{transaction_id}",
    response_model=TransactionEvent,
    dependencies=[
        Depends(require_roles("payment_channel", "dispatcher", "controller", "remitter_service"))
    ],
)
def contract_payment_status(transaction_id: str, request: Request):
    txn = request.app.state.remitter._reload(transaction_id)
    if txn is None:
        raise SafetyViolation(ErrorCode.TRANSACTION_NOT_FOUND, "transaction not found", 404)
    return transaction_view_to_event(txn, experiment_id=_experiment(request))


# ---------------------------------------------------------------- switch
switch_v1 = APIRouter(prefix="/v1", tags=["team-contract"])


class ContractStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transaction_id: str
    route_id: str
    check_id: str


@switch_v1.post("/status-checks", response_model=StatusCheckResult)
def contract_status_check(
    body: ContractStatusRequest,
    request: Request,
    x_bank_id: str = Header(),
    ident: VerifiedIdentity = Depends(require_roles("remitter_bank")),
):
    from backend.aorts.banking.switch.service import SwitchError

    if x_bank_id != ident.identity:  # the claimed bank must be the authenticated identity
        return error_response(
            ErrorCode.IDENTITY_MISMATCH, "x-bank-id does not match the signer", 403
        )
    svc = request.app.state.switch
    started = time.perf_counter()
    try:
        res = svc.check_status(
            StatusCheckRequest(
                transaction_id=body.transaction_id, bank_id=x_bank_id, route_id=body.route_id
            ),
            ident,
        )
    except SwitchError as err:
        status = 429 if err.status == 429 else err.status
        return error_response(
            err.code,
            err.message,
            status,
            retryable=status in (429, 503),
            details={"retry_after": err.retry_after},
        )
    with ctx_of(request).db.session() as s:
        rec = s.get(SwitchTransaction, body.transaction_id)
    return status_to_result(
        transaction_id=body.transaction_id,
        route_id=body.route_id,
        check_id=body.check_id,
        status=res.status.value,
        version=rec.version if rec else 1,
        latency_ms=(time.perf_counter() - started) * 1000,
        evidence_source_id="SWITCH",
    )


# ---------------------------------------------------------------- beneficiary
beneficiary_v1 = APIRouter(prefix="/v1", tags=["team-contract"])


@beneficiary_v1.get(
    "/mock/status/{transaction_id}",
    response_model=StatusCheckResult,
    dependencies=[Depends(require_roles("switch"))],
)
def contract_beneficiary_status(
    transaction_id: str, request: Request, route_id: str = Query("unknown")
):
    svc = request.app.state.beneficiary
    started = time.perf_counter()
    try:
        status, _ = svc.status(transaction_id)
    except Exception:  # noqa: BLE001 - injected faults: report an explicit, non-authoritative unknown
        status = None
    return status_to_result(
        transaction_id=transaction_id,
        route_id=route_id,
        check_id=f"bene-{svc.bank_id}-{transaction_id}",
        status=status.value if status else "PENDING",
        version=1,
        latency_ms=(time.perf_counter() - started) * 1000,
        evidence_source_id=f"beneficiary-{svc.bank_id}",
    )


# ---------------------------------------------------------------- control
control_v1 = APIRouter(prefix="/v1", tags=["team-contract"])


@control_v1.get("/events", response_model=list[TransactionEvent])
def contract_events(
    request: Request, after_id: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=1000)
):
    """Recorded transaction lifecycle events (from the transactional outbox) as contract messages."""
    with ctx_of(request).db.session() as s:
        rows = s.execute(
            select(OutboxEvent.id, OutboxEvent.payload)
            .where(OutboxEvent.id > after_id)
            .order_by(OutboxEvent.id)
            .limit(limit)
        ).all()
    out = []
    for _rid, payload in rows:
        ev = domain_event_to_contract(payload, experiment_id=_experiment(request))
        if ev is not None:
            out.append(ev)
    resp = JSONResponse([e.model_dump(mode="json") for e in out])
    resp.headers["X-Next-After-Id"] = str(rows[-1][0] if rows else after_id)
    return resp


@control_v1.post("/telemetry")
def contract_telemetry(
    obs: RouteTelemetry,
    request: Request,
    ident: VerifiedIdentity = Depends(require_roles("telemetry_source")),
):
    """Accept a contract ``RouteTelemetry`` and run it through Track B's telemetry validator."""
    mapped = TrackBTelemetry(
        event_id=f"{obs.source_id}-{obs.route_id}-{obs.sequence}"[:80],
        source_id=obs.source_id,
        route_id=obs.route_id,
        sequence=obs.sequence,
        timestamp=obs.timestamp,
        latency_p95_ms=obs.p95_latency_ms,
        throughput_tps=obs.observed_throughput_rps,
        timeout_rate=obs.timeout_rate,
        backlog=obs.queue_depth,
        correlation_id=None,
    )
    return request.app.state.telemetry.ingest(
        mapped, authenticated_source=ident.identity, experiment_id=obs.experiment_id
    ).as_dict()
