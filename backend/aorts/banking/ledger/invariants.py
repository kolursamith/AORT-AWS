"""Transaction state machine and the rule-based financial-safety invariants.

The digital twin / optimizer only *recommends*. Every rule here is evaluated by
Track B against the authoritative PostgreSQL row, inside the same database
transaction that would apply the change (no time-of-check/time-of-use gap).

Rule letters match the project brief (A-J).
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.aorts.contracts.enums import (
    ErrorCode,
    FinancialOutcome,
    SwitchStatus,
    TxnState,
    WorkflowState,
)

S = TxnState

# B. Valid state transitions: explicit allow-list, everything else is illegal.
TRANSITIONS: dict[TxnState, frozenset[TxnState]] = {
    S.DEBITED: frozenset({S.CONFIRMED_SUCCESS, S.CONFIRMED_FAILED, S.AMBIGUOUS}),
    S.AMBIGUOUS: frozenset(
        {S.CHECK_IN_FLIGHT, S.CONFIRMED_SUCCESS, S.CONFIRMED_FAILED, S.HANDOFF_UDIR, S.QUARANTINED}
    ),
    S.CHECK_IN_FLIGHT: frozenset(
        {S.CONFIRMED_SUCCESS, S.CONFIRMED_FAILED, S.AMBIGUOUS, S.HANDOFF_UDIR}
    ),
    S.CONFIRMED_FAILED: frozenset({S.REVERSAL_INITIATED}),
    S.REVERSAL_INITIATED: frozenset({S.REVERSED}),
    S.CONFIRMED_SUCCESS: frozenset({S.CLOSED}),
    S.HANDOFF_UDIR: frozenset({S.RESOLVED_BY_RECONCILIATION}),
    S.QUARANTINED: frozenset({S.AMBIGUOUS, S.HANDOFF_UDIR}),
    S.REVERSED: frozenset(),
    S.CLOSED: frozenset(),
    S.RESOLVED_BY_RECONCILIATION: frozenset(),
}

# Separation of the financial outcome from the workflow state.
PROJECTION: dict[TxnState, tuple[WorkflowState, FinancialOutcome]] = {
    S.DEBITED: (WorkflowState.ACTIVE, FinancialOutcome.UNKNOWN),
    S.AMBIGUOUS: (WorkflowState.AMBIGUOUS, FinancialOutcome.UNKNOWN),
    S.CHECK_IN_FLIGHT: (WorkflowState.CHECK_IN_FLIGHT, FinancialOutcome.UNKNOWN),
    S.CONFIRMED_SUCCESS: (WorkflowState.ACTIVE, FinancialOutcome.SUCCESS),
    S.CONFIRMED_FAILED: (WorkflowState.ACTIVE, FinancialOutcome.FAILURE),
    S.REVERSAL_INITIATED: (WorkflowState.ACTIVE, FinancialOutcome.FAILURE),
    S.REVERSED: (WorkflowState.CLOSED, FinancialOutcome.REVERSED),
    S.CLOSED: (WorkflowState.CLOSED, FinancialOutcome.SUCCESS),
    S.HANDOFF_UDIR: (WorkflowState.ESCALATED, FinancialOutcome.UNKNOWN),
    # Outcome of a reconciled transaction is set from the reconciliation evidence.
    S.RESOLVED_BY_RECONCILIATION: (WorkflowState.CLOSED, FinancialOutcome.UNKNOWN),
    # I. Quarantine is NOT a financial outcome.
    S.QUARANTINED: (WorkflowState.QUARANTINED, FinancialOutcome.UNKNOWN),
}

UNRESOLVED = frozenset({S.DEBITED, S.AMBIGUOUS, S.CHECK_IN_FLIGHT})
TERMINAL = frozenset({S.CLOSED, S.REVERSED, S.RESOLVED_BY_RECONCILIATION})
TRUSTED_EVIDENCE_SOURCES = frozenset({"SWITCH"})


class SafetyViolation(Exception):
    def __init__(
        self, code: ErrorCode, message: str, http_status: int = 409, details: dict | None = None
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.details = details or {}


def is_legal(current: TxnState | str, new: TxnState | str) -> bool:
    return TxnState(new) in TRANSITIONS[TxnState(current)]


def assert_transition(current: TxnState | str, new: TxnState | str) -> None:
    if not is_legal(current, new):
        raise SafetyViolation(
            ErrorCode.ILLEGAL_TRANSITION,
            f"illegal transition {current} -> {new}",
            details={"from": str(current), "to": str(new)},
        )


def assert_version(actual: int, expected: int | None) -> None:
    """B. Optimistic concurrency: stale versions are rejected, never merged."""
    if expected is not None and actual != expected:
        raise SafetyViolation(
            ErrorCode.STALE_VERSION,
            f"expected version {expected}, current {actual}",
            details={"expected_version": expected, "actual_version": actual},
        )


@dataclass(frozen=True)
class EvidenceCheck:
    ok: bool
    code: ErrorCode | None = None
    reason: str = ""


def check_evidence(txn_id: str, evidence) -> EvidenceCheck:
    """C + G. Only an authenticated, correctly-linked switch statement is evidence.

    A timeout, a lost response, a PENDING/NOT_FOUND answer, or an unauthenticated
    message is never evidence of failure.
    """
    if evidence is None:
        return EvidenceCheck(False, ErrorCode.INSUFFICIENT_EVIDENCE, "no evidence (e.g. timeout)")
    if evidence.transaction_id != txn_id:
        return EvidenceCheck(
            False,
            ErrorCode.WRONG_TRANSACTION,
            f"evidence is about {evidence.transaction_id}, not {txn_id}",
        )
    if not evidence.authenticated or evidence.source not in TRUSTED_EVIDENCE_SOURCES:
        return EvidenceCheck(
            False,
            ErrorCode.INSUFFICIENT_EVIDENCE,
            f"evidence source '{evidence.source}' is not an authenticated authority",
        )
    if evidence.status not in (SwitchStatus.SUCCESS, SwitchStatus.FAILED):
        return EvidenceCheck(
            False,
            ErrorCode.INSUFFICIENT_EVIDENCE,
            f"status {evidence.status} does not establish a financial outcome",
        )
    return EvidenceCheck(True)


def journal_is_balanced(entries) -> bool:
    """D. Double-entry: total debits == total credits for one journal (operation)."""
    debits = sum(e.amount_paise for e in entries if e.direction == "DEBIT")
    credits = sum(e.amount_paise for e in entries if e.direction == "CREDIT")
    return debits == credits and debits > 0


def expected_sender_net(state: TxnState | str, amount: int) -> int | None:
    """E. Transaction-level conservation: what the sender's net ledger effect must be."""
    state = TxnState(state)
    if state in (S.REVERSED,):
        return 0
    if state in (
        S.CLOSED,
        S.CONFIRMED_SUCCESS,
        S.DEBITED,
        S.AMBIGUOUS,
        S.CHECK_IN_FLIGHT,
        S.CONFIRMED_FAILED,
        S.REVERSAL_INITIATED,
        S.HANDOFF_UDIR,
        S.QUARANTINED,
    ):
        return -amount
    return None  # reconciled: depends on the reconciliation outcome
