"""HTTP routes for the mock payment switch."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from backend.aorts.app.dependencies import ctx_of, error_response, require_admin, require_roles
from backend.aorts.banking.switch.service import ResponseLost, SwitchError, SwitchService
from backend.aorts.contracts.enums import ErrorCode
from backend.aorts.contracts.requests import StatusCheckRequest, SwitchPayRequest
from backend.aorts.contracts.responses import QuotaView
from backend.aorts.security.request_auth import VerifiedIdentity, sign_response

router = APIRouter(prefix="/api/v1")


def svc(request: Request) -> SwitchService:
    return request.app.state.switch


def signed_json(
    request: Request, status: int, content: dict, headers: dict | None = None
) -> Response:
    """Sign the response so the remitter can authenticate the evidence source (rule G)."""
    body = json.dumps(content, separators=(",", ":"), default=str).encode()
    key = ctx_of(request).registry.signing_key_for("SWITCH")
    hdrs = {**sign_response(key, status, request.url.path, body), **(headers or {})}
    return Response(content=body, status_code=status, media_type="application/json", headers=hdrs)


def _switch_error(err: SwitchError) -> JSONResponse:
    headers = {"Retry-After": f"{max(1, round(err.retry_after))}"} if err.retry_after else None
    return error_response(
        err.code,
        err.message,
        err.status,
        retryable=err.status in (429, 503),
        details={"retry_after": err.retry_after},
        headers=headers,
    )


@router.post("/pay")
def pay(
    req: SwitchPayRequest,
    request: Request,
    ident: VerifiedIdentity = Depends(require_roles("remitter_bank")),
):
    try:
        result = svc(request).pay(req, ident)
    except SwitchError as err:
        return _switch_error(err)
    except ResponseLost:
        return error_response(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "upstream response lost (injected fault)",
            504,
            retryable=True,
        )
    return signed_json(request, 200, result.model_dump(mode="json"))


@router.post("/check-status")
def check_status(
    req: StatusCheckRequest,
    request: Request,
    ident: VerifiedIdentity = Depends(require_roles("remitter_bank")),
):
    try:
        result = svc(request).check_status(req, ident)
    except SwitchError as err:
        return _switch_error(err)
    return signed_json(request, 200, result.model_dump(mode="json"))


@router.post("/reconciliation/status")
def reconciliation_status(
    req: StatusCheckRequest,
    request: Request,
    ident: VerifiedIdentity = Depends(require_roles("remitter_bank")),
):
    """Low-rate reconciliation query for escalated (HANDOFF_UDIR) transactions. Signed like check-status."""
    try:
        result = svc(request).reconciliation_status(req, ident)
    except SwitchError as err:
        return _switch_error(err)
    return signed_json(request, 200, result.model_dump(mode="json"))


@router.get("/health")
def health(request: Request):
    s = svc(request)
    return {"status": "ok", "service": "switch", "destinations": s.destinations}


@router.get("/metrics")
def metrics(request: Request):
    return svc(request).metrics_view()


@router.get("/metrics/timeseries")
def timeseries(request: Request, seconds: int = 120):
    return {"series": ctx_of(request).metrics.series(min(max(seconds, 1), 600))}


@router.get("/metrics/routes")
def route_measurements(request: Request, window_s: float = 30.0):
    return {"routes": svc(request).route_measurements(window_s)}


@router.get("/routes")
def routes(request: Request):
    s = svc(request)
    banks = ["A1", "A2", "A3"]
    return {
        "routes": [f"{a}-{d}" for a in banks for d in s.destinations],
        "capacity": s.capacity.view(),
    }


@router.get("/security/banks/{bank_id}/quota", response_model=QuotaView)
def quota(bank_id: str, request: Request):
    s = svc(request)
    view = s.admission.quota_view(bank_id)
    return QuotaView(**view, defenses=ctx_of(request).runtime.defenses().model_dump())


@router.get("/admin/records", dependencies=[Depends(require_admin)])
def records(request: Request, limit: int = 100):
    return {"records": svc(request).all_records(min(limit, 500))}


@router.post("/admin/security/reset", dependencies=[Depends(require_admin)])
def reset_security(request: Request):
    """Refill all buckets/credits and clear in-memory counters between controlled experiments."""
    s = svc(request)
    s.admission.reset()
    ctx_of(request).metrics.reset()
    return {"reset": True}


@router.post("/admin/tier2/allocate")
def allocate(
    body: dict,
    request: Request,
    ident: VerifiedIdentity = Depends(require_roles("controller", "dispatcher")),
):
    total = float(body.get("total_checks_per_sec", 0))
    backlog = {str(k): int(v) for k, v in (body.get("backlog_by_bank") or {}).items()}
    if total <= 0 or total > 1000 or not backlog:
        return error_response(
            ErrorCode.VALIDATION_ERROR,
            "total_checks_per_sec (0,1000] and backlog_by_bank required",
            422,
        )
    with svc(request).admission._lock:
        alloc = svc(request).admission.credits.allocate(total, backlog)
    return {"allocation": alloc, "by": ident.identity}
