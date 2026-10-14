from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from contracts.v1.models import (
    EventType,
    FinancialOutcome,
    RecoveryDecision,
    StatusCheckResult,
    StatusOutcome,
    TransactionEvent,
    WorkflowState,
)


def test_transaction_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError, match="UTC offset"):
        TransactionEvent(
            event_id="evt-1",
            experiment_id="exp-1",
            transaction_id="txn-1",
            bank_id="bank-1",
            route_id="route-1",
            amount_minor=100,
            currency="INR",
            event_type=EventType.PAYMENT_TIMEOUT,
            financial_outcome=FinancialOutcome.UNKNOWN,
            workflow_state=WorkflowState.AMBIGUOUS,
            version=1,
            occurred_at=datetime(2026, 1, 1),
        )


def test_decision_rejects_duplicate_transactions() -> None:
    with pytest.raises(ValidationError, match="unique"):
        RecoveryDecision(
            experiment_id="exp-1",
            route_id="route-1",
            decision_id="decision-1",
            budget_rps=1,
            selected_transaction_ids=["txn-1", "txn-1"],
            predicted_delay_ms=5,
            predicted_new_ambiguous=0,
            decision_reason="test",
            expires_at=datetime.now(UTC) + timedelta(seconds=5),
        )


def test_authoritative_result_requires_evidence() -> None:
    with pytest.raises(ValidationError, match="evidence"):
        StatusCheckResult(
            transaction_id="txn-1",
            route_id="route-1",
            check_id="check-1",
            outcome=StatusOutcome.CREDITED,
            authoritative=True,
            response_version=1,
            latency_ms=5,
        )
