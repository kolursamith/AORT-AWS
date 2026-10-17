"""Execution-time financial safety gate.

Called by ``LedgerService._post`` immediately before every financial mutation, inside the same
PostgreSQL transaction that holds the row lock. It re-reads the AUTHORITATIVE row (flushed state
of this transaction, not a cached ORM object) and refuses the posting unless:

* SETTLE   - the outcome is SUCCESS (CONFIRMED_SUCCESS) or, for an escalated transaction, the
             reconciliation evidence is an authenticated SWITCH SUCCESS;
* REVERSAL - the outcome is FAILURE (set only by authenticated FAILED evidence) or, for an escalated
             transaction, the reconciliation evidence is an authenticated SWITCH FAILED;
* DEBIT    - the transaction is freshly DEBITED at version 1;
* any op   - no operation with the same id, and no operation of the same type for the same
             (transaction, bank), already exists (uniqueness re-checked; DB constraints remain the
             last line of defence);
* evidence - a timeout / missing / unauthenticated answer is never accepted as evidence.

Every decision is counted; rejections raise ``SafetyViolation`` and are audited by the caller.
"""

from __future__ import annotations

from collections import Counter

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from backend.aorts.banking.ledger.invariants import TRUSTED_EVIDENCE_SOURCES, SafetyViolation
from backend.aorts.banking.ledger.models import FinancialOperation, Transaction
from backend.aorts.contracts.enums import ErrorCode

GATE_STATS: Counter = Counter()


def _authentic(evidence: dict | None, status: str) -> bool:
    return (
        bool(evidence)
        and evidence.get("authenticated") is True
        and evidence.get("source") in TRUSTED_EVIDENCE_SOURCES
        and evidence.get("status") == status
    )


def check_before_mutation(
    s: Session, *, txn_id: str, bank_id: str, op_type: str, op_id: str, evidence: dict | None
) -> dict:
    """Raise SafetyViolation unless this mutation is justified right now. Returns the checked facts."""
    s.flush()  # make this transaction's own pending state visible to the authoritative re-read
    facts = {"op_type": op_type, "operation_id": op_id}
    if op_type in ("DEBIT", "SETTLE", "REVERSAL"):
        row = s.execute(
            select(Transaction.state, Transaction.financial_outcome, Transaction.version)
            .where(Transaction.transaction_id == txn_id)
            .with_for_update()
        ).one_or_none()
        if row is None:
            _reject(
                "TRANSACTION_MISSING", ErrorCode.TRANSACTION_NOT_FOUND, f"no transaction {txn_id}"
            )
        state, outcome, version = row
        facts.update(state=state, financial_outcome=outcome, version=version)
        if op_type == "DEBIT" and not (state == "DEBITED" and version == 1):
            _reject("DEBIT_STATE", ErrorCode.ILLEGAL_TRANSITION, f"debit on {state} v{version}")
        if op_type == "SETTLE" and not (
            outcome == "SUCCESS" or (state == "HANDOFF_UDIR" and _authentic(evidence, "SUCCESS"))
        ):
            _reject(
                "SETTLE_EVIDENCE",
                ErrorCode.INSUFFICIENT_EVIDENCE,
                f"settlement needs authenticated SUCCESS (state {state}, outcome {outcome})",
            )
        if op_type == "REVERSAL" and not (
            outcome == "FAILURE" or (state == "HANDOFF_UDIR" and _authentic(evidence, "FAILED"))
        ):
            _reject(
                "REVERSAL_EVIDENCE",
                ErrorCode.INSUFFICIENT_EVIDENCE,
                f"reversal needs authenticated FAILED (state {state}, outcome {outcome}); "
                f"a timeout is never evidence",
            )
    dup = s.execute(
        select(FinancialOperation.operation_id).where(
            or_(
                FinancialOperation.operation_id == op_id,
                (FinancialOperation.transaction_id == txn_id)
                & (FinancialOperation.operation_type == op_type)
                & (FinancialOperation.bank_id == bank_id),
            )
        )
    ).first()
    if dup is not None:
        _reject(
            "DUPLICATE_OPERATION",
            ErrorCode.ALREADY_APPLIED,
            f"{op_type} already applied for {txn_id} (operation {dup[0]})",
        )
    GATE_STATS["passed"] += 1
    GATE_STATS[f"passed:{op_type}"] += 1
    return facts


def _reject(label: str, code: ErrorCode, message: str):
    GATE_STATS["rejected"] += 1
    GATE_STATS[f"rejected:{label}"] += 1
    raise SafetyViolation(code, f"financial safety gate: {message}", 409, {"gate": label})
