from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.ledger.models import Base, LedgerEntry, Transaction
from backend.ledger.operations import Posting, advance_transaction, apply_balanced_operation


def test_operation_is_balanced_idempotent_and_atomic() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Transaction(transaction_id="txn-1", workflow_state="ambiguous", version=1))
        session.commit()
        postings = [Posting("payer", -500), Posting("payee", 500)]
        assert apply_balanced_operation(
            session, operation_id="operation-1", transaction_id="txn-1", postings=postings
        )
        assert not apply_balanced_operation(
            session, operation_id="operation-1", transaction_id="txn-1", postings=postings
        )
        assert len(list(session.scalars(select(LedgerEntry)))) == 2


def test_stale_version_cannot_overwrite() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Transaction(transaction_id="txn-1", workflow_state="ambiguous", version=2))
        session.commit()
        assert not advance_transaction(session, "txn-1", 1, "resolved", "credited")
