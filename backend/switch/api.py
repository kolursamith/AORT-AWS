"""Mock switch status API with per-bank quotas and per-route credits."""

import os

from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel

from backend.security.rate_limits import PerBankRateLimiter
from backend.switch.credit_coordinator import RouteCredits
from contracts.v1.models import StatusCheckResult, StatusOutcome

app = FastAPI(title="AORT mock payment switch", version="0.1.0")
limiter = PerBankRateLimiter(quota=int(os.getenv("AORT_BANK_CHECK_QUOTA", "10")))
credits = RouteCredits()
credits.replace("route-1", int(os.getenv("AORT_FIXED_BUDGET_RPS", "5")))


class StatusRequest(BaseModel):
    transaction_id: str
    route_id: str
    check_id: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "switch", "mode": "local-mock"}


@app.post("/v1/status-checks", response_model=StatusCheckResult)
def request_status(request: StatusRequest, x_bank_id: str = Header()) -> StatusCheckResult:
    limit = limiter.check(x_bank_id)
    if not limit.accepted:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "BANK_RATE_LIMITED", "retry_after_seconds": limit.retry_after_seconds},
        )
    if not credits.consume(request.route_id):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "ROUTE_CREDIT_EXHAUSTED", "route_id": request.route_id},
        )
    # The starter switch has no beneficiary evidence client. It explicitly returns unknown.
    return StatusCheckResult(
        transaction_id=request.transaction_id,
        route_id=request.route_id,
        check_id=request.check_id,
        outcome=StatusOutcome.UNKNOWN,
        authoritative=False,
        response_version=1,
        latency_ms=0,
    )
