"""Isolated mock beneficiary; results are synthetic and explicitly identified."""

import hashlib

from fastapi import FastAPI

from contracts.v1.models import StatusCheckResult, StatusOutcome

app = FastAPI(title="AORT mock beneficiary", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "beneficiary", "data": "synthetic-only"}


@app.get("/v1/mock/status/{transaction_id}", response_model=StatusCheckResult)
def status(transaction_id: str, route_id: str = "route-1") -> StatusCheckResult:
    digest = hashlib.sha256(transaction_id.encode()).digest()
    outcome = StatusOutcome.CREDITED if digest[0] % 2 == 0 else StatusOutcome.NOT_CREDITED
    return StatusCheckResult(
        transaction_id=transaction_id,
        route_id=route_id,
        check_id=f"mock-check-{transaction_id}",
        outcome=outcome,
        authoritative=True,
        response_version=1,
        latency_ms=5,
        evidence_source_id="mock-beneficiary-1",
    )
