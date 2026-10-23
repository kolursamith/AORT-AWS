"""Cross-bank isolation at the switch: a bank can never fence, read or claim another bank's transaction.

Regression for a defect found by the replay experiment: a status check for an id the switch had never
seen wrote a FAILED "not received" tombstone owned by WHICHEVER bank asked, so bank A2 could fence
bank A1's payment and lock A1 out of its own status checks; a duplicate /pay also returned another
bank's record.
"""

from __future__ import annotations

import re

from backend.aorts.contracts.events import new_transaction_id, transaction_id_bank
from tests.trackb.conftest import count, pay


def _check(st, txn_id: str, signer: str, route: str):
    body = {"transaction_id": txn_id, "bank_id": signer, "route_id": route}
    return st.client.post("switch", "/api/v1/check-status", body, as_identity=signer)


def test_transaction_ids_are_namespaced_by_remitter_bank(stack):
    for sender, bank in (("A1-CUST-0001", "A1"), ("A2-CUST-0001", "A2")):
        t = pay(stack, sender=sender).json()["transaction"]
        assert re.fullmatch(rf"TXN{bank}-[0-9A-F]{{16}}", t["transaction_id"])
        assert transaction_id_bank(t["transaction_id"]) == bank
    assert transaction_id_bank("TXN-NOPE") is None  # legacy ids keep the old behaviour


def test_other_bank_cannot_fence_an_unseen_transaction(stack):
    t = pay(stack, simulate="timeout").json()["transaction"]  # never reached the switch
    tid = t["transaction_id"]
    r = _check(stack, tid, "A2", "A2-B")
    assert r.status_code == 403 and r.json()["error_code"] == "IDENTITY_MISMATCH"
    sql = "SELECT count(*) FROM switch_transactions WHERE transaction_id=:t"
    assert count(stack.db, sql, t=tid) == 0  # no tombstone written for A2
    own = _check(stack, tid, "A1", t["route_id"])  # the real owner still gets the fenced answer
    assert own.status_code == 200 and own.json()["status"] == "FAILED"


def test_other_bank_cannot_read_a_record_through_duplicate_pay(stack):
    t = pay(stack).json()["transaction"]
    body = {
        "transaction_id": t["transaction_id"],
        "remitter_bank_id": "A2",
        "beneficiary_bank_id": "B",
        "beneficiary_account_id": "B-CUST-0001",
        "route_id": "A2-B",
        "amount_paise": 100,
    }
    r = stack.client.post("switch", "/api/v1/pay", body, as_identity="A2")
    assert r.status_code == 403 and "status" not in r.json()


def test_same_bank_duplicate_pay_still_returns_its_own_record(stack):
    tid = new_transaction_id("A1")
    body = {
        "transaction_id": tid,
        "remitter_bank_id": "A1",
        "beneficiary_bank_id": "B",
        "beneficiary_account_id": "B-CUST-0001",
        "route_id": "A1-B",
        "amount_paise": 100,
    }
    first = stack.client.post("switch", "/api/v1/pay", body, as_identity="A1")
    again = stack.client.post("switch", "/api/v1/pay", body, as_identity="A1")
    assert first.status_code == 200 and again.status_code == 200
    assert again.json()["status"] == first.json()["status"]


def test_reconciliation_rejects_foreign_ids(stack):
    t = pay(stack, simulate="timeout").json()["transaction"]
    body = {"transaction_id": t["transaction_id"], "bank_id": "A2", "route_id": "A2-B"}
    r = stack.client.post("switch", "/api/v1/reconciliation/status", body, as_identity="A2")
    assert r.status_code == 403
