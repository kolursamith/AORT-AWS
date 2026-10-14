"""Mock remitter API; accepting a request never claims a completed transfer."""

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import FastAPI, status
from pydantic import BaseModel, Field

from contracts.v1.models import EventType, FinancialOutcome, TransactionEvent, WorkflowState

app = FastAPI(title="AORT mock remitter", version="0.1.0")


class PaymentRequest(BaseModel):
    bank_id: str
    route_id: str
    amount_minor: int = Field(ge=1)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    experiment_id: str = "local"


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "remitter", "data": "synthetic-only"}


@app.post("/v1/payments", response_model=TransactionEvent, status_code=status.HTTP_202_ACCEPTED)
def submit(request: PaymentRequest) -> TransactionEvent:
    transaction_id = f"txn-{uuid4().hex}"
    return TransactionEvent(
        event_id=f"evt-{uuid4().hex}",
        experiment_id=request.experiment_id,
        transaction_id=transaction_id,
        bank_id=request.bank_id,
        route_id=request.route_id,
        amount_minor=request.amount_minor,
        currency=request.currency,
        event_type=EventType.PAYMENT_INITIATED,
        financial_outcome=FinancialOutcome.UNKNOWN,
        workflow_state=WorkflowState.INITIATED,
        version=1,
        occurred_at=datetime.now(UTC),
    )
