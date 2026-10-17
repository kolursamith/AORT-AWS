"""AORTS controller + twins + safety gates against the real stack and PostgreSQL."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from backend.aorts.adapters.controller_client import ResilientController
from backend.aorts.adapters.twin_state import LocalVersionedStore
from backend.aorts.banking.dispatcher.local_adapter import LocalQueue
from backend.aorts.banking.dispatcher.service import Dispatcher
from backend.aorts.banking.ledger.invariants import SafetyViolation
from backend.aorts.banking.ledger.safety_gate import check_before_mutation
from backend.aorts.contracts.enums import ErrorCode
from backend.aorts.recovery.budget import BudgetConfig
from backend.aorts.recovery.controller import AortsController
from backend.aorts.twin.engine import TwinEngine
from tests.trackb.conftest import count, pay


def aorts_dispatcher(st, store=None):
    store = store or LocalVersionedStore()
    ctrl = AortsController(TwinEngine(st.db), store=store, budget_cfg=BudgetConfig(tick_s=1.0))
    d = Dispatcher(
        st.contexts["remitter"],
        st.remitter_service,
        controller=ResilientController(ctrl, 2),
        twin=store,
        queue=LocalQueue(2),
    )
    return d, ctrl, store


# ---------------------------------------------------------------- execution-time financial gate
def test_gate_refuses_reversal_without_authenticated_failure(stack):
    txn = pay(stack, simulate="timeout").json()["transaction"]  # AMBIGUOUS: outcome UNKNOWN
    with pytest.raises(SafetyViolation) as e:
        with stack.db.session() as s:
            check_before_mutation(
                s,
                txn_id=txn["transaction_id"],
                bank_id="A1",
                op_type="REVERSAL",
                op_id=f"{txn['transaction_id']}:REVERSAL",
                evidence=None,
            )
    assert (
        e.value.code == ErrorCode.INSUFFICIENT_EVIDENCE
        and e.value.details["gate"] == "REVERSAL_EVIDENCE"
    )


def test_gate_refuses_settlement_on_unauthenticated_reconciliation_evidence(stack):
    txn = pay(stack, simulate="timeout").json()["transaction"]
    stack.ledger.escalate(txn["transaction_id"], "test")
    forged = {"source": "SWITCH", "status": "SUCCESS", "authenticated": False}
    with pytest.raises(SafetyViolation) as e:
        with stack.db.session() as s:
            check_before_mutation(
                s,
                txn_id=txn["transaction_id"],
                bank_id="A1",
                op_type="SETTLE",
                op_id=f"{txn['transaction_id']}:SETTLE",
                evidence=forged,
            )
    assert e.value.details["gate"] == "SETTLE_EVIDENCE"


def test_gate_refuses_duplicate_operation(stack):
    txn = pay(stack).json()["transaction"]  # CLOSED: SETTLE already applied
    with pytest.raises(SafetyViolation) as e:
        with stack.db.session() as s:
            check_before_mutation(
                s,
                txn_id=txn["transaction_id"],
                bank_id="A1",
                op_type="SETTLE",
                op_id="ANOTHER-ID",
                evidence=None,
            )
    assert e.value.code == ErrorCode.ALREADY_APPLIED


def test_every_committed_operation_carries_gate_facts(stack):
    pay(stack)
    pay(stack, beneficiary="B-CUST-9999")
    stack.flush_audit()
    with stack.db.session() as s:
        rows = (
            s.execute(
                text(
                    "SELECT metadata FROM audit_events WHERE action LIKE '%\\_COMMITTED' ESCAPE '\\'"
                )
            )
            .scalars()
            .all()
        )
    assert rows and all("safety_gate" in r for r in rows)


# ---------------------------------------------------------------- AORTS controller end-to-end
def test_aorts_controller_resolves_backlog_with_explained_decisions(stack):
    for i in range(6):
        pay(stack, sender=f"A1-CUST-000{i + 1}", simulate="lost-response")
    d, ctrl, store = aorts_dispatcher(stack)
    for _ in range(10):
        if (
            count(
                stack.db,
                "SELECT count(*) FROM transactions WHERE state IN ('AMBIGUOUS','CHECK_IN_FLIGHT')",
            )
            == 0
        ):
            break
        rec = d.tick(inline=True)
    assert count(stack.db, "SELECT count(*) FROM transactions WHERE state='CLOSED'") == 6
    pred = ctrl.last_decision["predicted"]
    assert (
        pred["routes"]["A1-B"]["decision"]["candidates"]
        and pred["routes"]["A1-B"]["decision"]["reasons"]
    )
    assert all("components" in x and "reason" in x for x in pred["selection"])
    snap = store.get("aorts:snapshot")[1]
    assert "A1-B" in snap["routes"] and snap["last_decision"]["policy"] == "aorts-adaptive"
    assert "gate_refusals" in rec and stack.ledger.verify_ledger()["ok"]


def test_twin_reconstruction_from_recorded_events_matches_incremental(stack):
    live = TwinEngine(stack.db)
    for i in range(4):
        pay(stack, sender=f"A1-CUST-000{i + 1}", simulate="lost-response")
        live.poll()  # incremental, interleaved with activity
    pay(stack, beneficiary="B-CUST-9999")
    d, _, _ = aorts_dispatcher(stack)
    d.tick(inline=True)
    live.poll()
    rebuilt = TwinEngine(stack.db)
    rebuilt.rebuild()
    assert live.fingerprint() == rebuilt.fingerprint()
    assert rebuilt.txns.stats["applied"] > 10 and rebuilt.snapshot()["transactions_tracked"] == 5
