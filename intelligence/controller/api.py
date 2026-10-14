"""Standalone recovery controller API with no dependency on backend internals."""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import FastAPI
from pydantic import BaseModel, Field

from contracts.v1.models import RecoveryDecision
from intelligence.controller.fallback import safe_budget

app = FastAPI(title="AORT recovery controller", version="0.1.0")


class DecisionRequest(BaseModel):
    experiment_id: str
    route_id: str
    estimated_capacity_rps: float = Field(ge=0)
    confidence: float = Field(ge=0, le=1)
    pending_transaction_ids: list[str] = Field(default_factory=list, max_length=10_000)


@app.get("/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "controller",
        "mode": os.getenv("AORT_CONTROLLER_MODE", "fixed"),
    }


@app.post("/v1/recovery/decisions", response_model=RecoveryDecision)
def decide(request: DecisionRequest) -> RecoveryDecision:
    budget = safe_budget(request.estimated_capacity_rps, request.confidence)
    limit = int(budget)
    return RecoveryDecision(
        experiment_id=request.experiment_id,
        route_id=request.route_id,
        decision_id=f"decision-{uuid4().hex}",
        budget_rps=budget,
        selected_transaction_ids=request.pending_transaction_ids[:limit],
        predicted_delay_ms=0,
        predicted_new_ambiguous=0,
        decision_reason="conservative fallback; no financial outcome is inferred",
        expires_at=datetime.now(UTC) + timedelta(seconds=5),
    )
