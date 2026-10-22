"""Outbox atomicity and concurrent-debit safety against real PostgreSQL.

* A ledger change and its outbox event commit or roll back together (fault injected into the
  outbox insert).
* Every committed transaction version has exactly one recorded event and one outbox event.
* Concurrent payments from one sender can never overdraw the account.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.aorts.banking.ledger import service as ledger_service
from backend.aorts.contracts.requests import PaymentRequest
from tests.trackb.conftest import OPENING, balance, count, pay

SENDER = "A1-CUST-0003"


class _OutboxDown(RuntimeError):
    pass


def _request(amount: int = 10_000) -> PaymentRequest:
    return PaymentRequest(
        sender_account_id=SENDER,
        beneficiary_account_id="B-CUST-0003",
        beneficiary_bank_id="B",
        amount_paise=amount,
        idempotency_key=f"atom-{uuid.uuid4().hex}",
    )


def _totals(db) -> tuple[int, int, int]:
    return (
        count(db, "SELECT count(*) FROM transactions"),
        count(db, "SELECT count(*) FROM financial_operations"),
        count(db, "SELECT count(*) FROM outbox_events"),
    )


def test_debit_rolls_back_when_the_outbox_insert_fails(stack, monkeypatch):
    before, bal = _totals(stack.db), balance(stack.db, SENDER)

    def broken(*_a, **_k):
        raise _OutboxDown("outbox unavailable")

    monkeypatch.setattr(ledger_service, "add_outbox", broken)
    with pytest.raises(_OutboxDown):
        stack.ledger.create_payment(_request())
    assert _totals(stack.db) == before  # no transaction, no posting, no event
    assert balance(stack.db, SENDER) == bal


def test_state_transition_rolls_back_when_the_outbox_insert_fails(stack, monkeypatch):
    t = pay(stack, sender=SENDER, simulate="lost-response").json()["transaction"]
    tid = t["transaction_id"]
    state = count(stack.db, "SELECT version FROM transactions WHERE transaction_id=:t", t=tid)
    events = count(stack.db, "SELECT count(*) FROM outbox_events WHERE aggregate_id=:t", t=tid)

    def broken(*_a, **_k):
        raise _OutboxDown("outbox unavailable")

    monkeypatch.setattr(ledger_service, "add_outbox", broken)
    with pytest.raises(_OutboxDown):
        stack.ledger.start_check(tid, t["version"])
    assert (
        count(stack.db, "SELECT version FROM transactions WHERE transaction_id=:t", t=tid) == state
    )
    assert (
        count(stack.db, "SELECT count(*) FROM outbox_events WHERE aggregate_id=:t", t=tid) == events
    )


def test_rejected_payment_leaves_no_rows_at_all(stack):
    before = _totals(stack.db)
    r = pay(stack, sender=SENDER, amount=OPENING + 1)  # more than the balance
    assert r.status_code == 422 and r.json()["error_code"] == "INSUFFICIENT_FUNDS"
    assert _totals(stack.db) == before


def test_every_committed_version_has_exactly_one_event_and_one_outbox_row(stack):
    pay(stack, sender=SENDER)  # success path
    pay(stack, sender="A2-CUST-0003", simulate="lost-response")  # stays ambiguous
    pay(stack, sender="A3-CUST-0003", beneficiary="B-CUST-9999")  # fails and is reversed
    rows = count(
        stack.db,
        """SELECT count(*) FROM transactions t WHERE
             t.version <> (SELECT count(*) FROM transaction_events e WHERE e.transaction_id = t.transaction_id)
          OR t.version <> (SELECT count(*) FROM outbox_events o WHERE o.aggregate_id = t.transaction_id)
          OR t.version <> (SELECT count(DISTINCT (o.payload->>'transaction_version'))
                           FROM outbox_events o WHERE o.aggregate_id = t.transaction_id)""",
    )
    assert count(stack.db, "SELECT count(*) FROM transactions") == 3
    assert rows == 0


def test_concurrent_debits_cannot_overdraw_the_sender(stack):
    quarter = OPENING // 4
    with ThreadPoolExecutor(10) as pool:
        codes = list(
            pool.map(lambda _: pay(stack, sender=SENDER, amount=quarter).status_code, range(10))
        )
    accepted = codes.count(200) + codes.count(201) + codes.count(202)
    assert accepted == 4, codes  # exactly the four that fit in the balance
    assert codes.count(422) == 6, codes
    debits = count(
        stack.db,
        "SELECT coalesce(sum(o.amount_paise), 0) FROM financial_operations o "
        "JOIN transactions t ON t.transaction_id = o.transaction_id "
        "WHERE t.sender_account_id = :a AND o.operation_type = 'DEBIT'",
        a=SENDER,
    )
    assert debits == 4 * quarter
    assert balance(stack.db, SENDER) == OPENING - 4 * quarter >= 0
    assert stack.ledger.verify_ledger()["ok"]
