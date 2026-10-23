"""HTTP routes for a simulated beneficiary bank. Only the switch may call credit/fence."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from backend.aorts.app.dependencies import ctx_of, error_response, require_roles
from backend.aorts.banking.beneficiary.service import (
    BeneficiaryError,
    BeneficiaryService,
    BeneficiaryUnavailable,
    ResponseLost,
)
from backend.aorts.contracts.enums import ErrorCode
from backend.aorts.contracts.requests import BeneficiaryCreditRequest

router = APIRouter(prefix="/api/v1")


def svc(request: Request) -> BeneficiaryService:
    return request.app.state.beneficiary


@router.post("/beneficiary/credit", dependencies=[Depends(require_roles("switch"))])
def credit(req: BeneficiaryCreditRequest, request: Request):
    try:
        return svc(request).credit(req).model_dump(mode="json")
    except BeneficiaryUnavailable:
        return error_response(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "beneficiary bank unavailable (injected)",
            503,
            retryable=True,
        )
    except BeneficiaryError:
        return error_response(
            ErrorCode.INTERNAL_ERROR, "beneficiary processing error (injected)", 500, retryable=True
        )
    except ResponseLost:
        return error_response(
            ErrorCode.DEPENDENCY_UNAVAILABLE, "response lost (injected)", 504, retryable=True
        )


def _query(request: Request, transaction_id: str, fn):
    try:
        status, reason = fn(transaction_id)
    except BeneficiaryUnavailable:
        return error_response(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "beneficiary bank unavailable (injected)",
            503,
            retryable=True,
        )
    except BeneficiaryError:
        return error_response(
            ErrorCode.INTERNAL_ERROR, "beneficiary processing error (injected)", 500, retryable=True
        )
    return {
        "transaction_id": transaction_id,
        "bank_id": svc(request).bank_id,
        "status": status.value,
        "reason": reason,
    }


@router.get(
    "/beneficiary/transactions/{transaction_id}", dependencies=[Depends(require_roles("switch"))]
)
def get_status(transaction_id: str, request: Request):
    return _query(request, transaction_id, svc(request).status)


@router.post(
    "/beneficiary/transactions/{transaction_id}/fence",
    dependencies=[Depends(require_roles("switch"))],
)
def fence(transaction_id: str, request: Request):
    return _query(request, transaction_id, svc(request).fence)


@router.get("/health")
def health(request: Request):
    return {"status": "ok", "service": f"beneficiary-{svc(request).bank_id}"}


@router.get("/metrics")
def metrics(request: Request):
    return {"bank_id": svc(request).bank_id, **ctx_of(request).metrics.snapshot()}
