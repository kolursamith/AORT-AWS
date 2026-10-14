"""Atomic, balanced, idempotent ledger operations."""

from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.ledger.models import FinancialOperation, LedgerEntry, OutboxEvent, Transaction


@dataclass(frozen=True)
class Posting:
    account_id: str
    amount_minor: int


def apply_balanced_operation(
    session: Session,
    *,
    operation_id: str,
    transaction_id: str,
    postings: list[Posting],
) -> bool:
    if not postings or sum(posting.amount_minor for posting in postings) != 0:
        raise ValueError("ledger postings must be nonempty and balanced")
    if session.get(Transaction, transaction_id) is None:
        raise ValueError("transaction does not exist")
    try:
        with session.begin_nested():
            session.add(
                FinancialOperation(operation_id=operation_id, transaction_id=transaction_id)
            )
            session.flush()
            session.add_all(
                LedgerEntry(
                    operation_id=operation_id,
                    account_id=posting.account_id,
                    amount_minor=posting.amount_minor,
                )
                for posting in postings
            )
            session.add(
                OutboxEvent(
                    operation_id=operation_id,
                    event_type="ledger.operation_applied",
                    payload={"transaction_id": transaction_id},
                )
            )
        session.commit()
        return True
    except IntegrityError:
        session.rollback()
        existing = session.scalar(
            select(FinancialOperation).where(FinancialOperation.operation_id == operation_id)
        )
        if existing is not None:
            return False
        raise


def advance_transaction(
    session: Session,
    transaction_id: str,
    expected_version: int,
    new_state: str,
    new_outcome: str,
) -> bool:
    result = session.execute(
        update(Transaction)
        .where(
            Transaction.transaction_id == transaction_id, Transaction.version == expected_version
        )
        .values(
            workflow_state=new_state, financial_outcome=new_outcome, version=expected_version + 1
        )
    )
    if result.rowcount != 1:
        session.rollback()
        return False
    session.commit()
    return True
