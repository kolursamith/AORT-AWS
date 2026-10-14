"""Monotonic transaction lifecycle projection."""

from dataclasses import dataclass

from contracts.v1.models import TransactionEvent, WorkflowState

ALLOWED = {
    WorkflowState.INITIATED: {WorkflowState.AMBIGUOUS, WorkflowState.RESOLVED},
    WorkflowState.AMBIGUOUS: {
        WorkflowState.CHECK_PENDING,
        WorkflowState.RESOLVED,
        WorkflowState.ESCALATED,
    },
    WorkflowState.CHECK_PENDING: {
        WorkflowState.AMBIGUOUS,
        WorkflowState.RESOLVED,
        WorkflowState.ESCALATED,
    },
    WorkflowState.RESOLVED: set(),
    WorkflowState.ESCALATED: {WorkflowState.RESOLVED},
}


@dataclass
class TransactionTwin:
    transaction_id: str
    state: WorkflowState
    version: int

    def apply(self, event: TransactionEvent) -> None:
        if event.transaction_id != self.transaction_id:
            raise ValueError("event belongs to another transaction")
        if event.version <= self.version:
            raise ValueError("stale transaction version")
        if event.workflow_state not in ALLOWED[self.state]:
            raise ValueError(f"invalid transition {self.state} -> {event.workflow_state}")
        self.state = event.workflow_state
        self.version = event.version
