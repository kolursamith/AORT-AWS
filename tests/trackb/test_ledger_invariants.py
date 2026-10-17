"""B. Ledger + invariant tests (rules A-J)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backend.aorts.banking.ledger import invariants as inv
from backend.aorts.banking.ledger.invariants import SafetyViolation
from backend.aorts.contracts.enums import ErrorCode, SwitchStatus, TxnState
from backend.aorts.contracts.requests import StatusEvidence
from tests.trackb.conftest import OPENING, balance, count, pay


def evidence(txn_id, status, *, source="SWITCH", authenticated=True):
    return StatusEvidence(
        transaction_id=txn_id,
        status=status,
        source=source,
        observed_at=datetime.now(UTC),
        authenticated=authenticated,
    )


def test_transition_map_rejects_illegal_moves():
    assert inv.is_legal(TxnState.AMBIGUOUS, TxnState.CHECK_IN_FLIGHT)
    for frm, to in [
        (TxnState.CLOSED, TxnState.REVERSED),
        (TxnState.REVERSED, TxnState.CLOSED),
        (TxnState.DEBITED, TxnState.REVERSED),
        (TxnState.AMBIGUOUS, TxnState.REVERSAL_INITIATED),
        (TxnState.CONFIRMED_SUCCESS, TxnState.CONFIRMED_FAILED),
        (TxnState.QUARANTINED, TxnState.CLOSED),
    ]:
        with pytest.raises(SafetyViolation) as e:
            inv.assert_transition(frm, to)
        assert e.value.code == ErrorCode.ILLEGAL_TRANSITION


def test_every_state_has_a_projection_and_quarantine_is_not_an_outcome():
    assert set(inv.PROJECTION) == set(TxnState)
    assert inv.PROJECTION[TxnState.QUARANTINED][1].value == "UNKNOWN"


def test_timeout_is_not_evidence():
    assert not inv.check_evidence("T1", None).ok
    assert not inv.check_evidence("T1", evidence("T1", SwitchStatus.PENDING)).ok
    assert not inv.check_evidence("T1", evidence("T2", SwitchStatus.FAILED)).ok
    assert not inv.check_evidence("T1", evidence("T1", SwitchStatus.FAILED, authenticated=False)).ok
    assert not inv.check_evidence("T1", evidence("T1", SwitchStatus.FAILED, source="B")).ok
    assert inv.check_evidence("T1", evidence("T1", SwitchStatus.FAILED)).ok


def test_unknown_outcome_does_not_permit_reversal(stack):
    txn = pay(stack, simulate="timeout").json()["transaction"]
    with pytest.raises(SafetyViolation) as e:
        stack.ledger.reverse(
            txn["transaction_id"],
            f"{txn['transaction_id']}:REVERSAL",
            expected_version=txn["version"],
            actor="CONTROLLER",
            reason="blind reversal attempt",
        )
    assert e.value.code == ErrorCode.INSUFFICIENT_EVIDENCE
    assert balance(stack.db, "A1-CUST-0001") == OPENING - 10_000
    assert stack.ledger.eligibility(txn["transaction_id"], "REVERSAL").eligible is False


def test_unauthenticated_failure_evidence_rejected(stack):
    txn = pay(stack, simulate="timeout").json()["transaction"]
    with pytest.raises(SafetyViolation):
        stack.ledger.apply_evidence(
            txn["transaction_id"],
            evidence(txn["transaction_id"], SwitchStatus.FAILED, authenticated=False),
            actor="X",
        )
    assert (
        stack.ledger.eligibility(txn["transaction_id"], "STATUS_CHECK").current_state == "AMBIGUOUS"
    )


def test_pending_evidence_preserves_uncertainty(stack):
    txn = pay(stack, simulate="timeout").json()["transaction"]
    res = stack.ledger.apply_evidence(
        txn["transaction_id"], evidence(txn["transaction_id"], SwitchStatus.PENDING), actor="SWITCH"
    )
    assert (
        res.decision == "DEFERRED"
        and res.state == "AMBIGUOUS"
        and res.financial_outcome == "UNKNOWN"
    )


def test_reversal_exactly_once_and_balanced(stack_factory):
    st = stack_factory(auto_reverse_on_confirmed_failure=False)
    txn = pay(st, simulate="timeout", amount=9_000).json()["transaction"]
    st.ledger.apply_evidence(
        txn["transaction_id"], evidence(txn["transaction_id"], SwitchStatus.FAILED), actor="SWITCH"
    )
    v = st.ledger.eligibility(txn["transaction_id"], "REVERSAL").current_version
    op = f"{txn['transaction_id']}:REVERSAL"
    first = st.ledger.reverse(txn["transaction_id"], op, expected_version=v, actor="C", reason="r")
    second = st.ledger.reverse(txn["transaction_id"], op, expected_version=v, actor="C", reason="r")
    assert first.decision == "ACCEPTED" and second.decision == "DUPLICATE"
    assert balance(st.db, "A1-CUST-0001") == OPENING
    assert count(st.db, "SELECT count(*) FROM ledger_entries WHERE operation_id=:o", o=op) == 2
    # F. causal link: the reversal references the original debit.
    assert (
        count(
            st.db,
            "SELECT count(*) FROM financial_operations WHERE operation_id=:o AND "
            "references_operation_id=:d",
            o=op,
            d=f"{txn['transaction_id']}:DEBIT",
        )
        == 1
    )
    assert st.ledger.verify_ledger()["ok"]


def test_operation_id_cannot_be_reused_for_another_transaction(stack_factory):
    st = stack_factory(auto_reverse_on_confirmed_failure=False)
    a = pay(st, simulate="timeout").json()["transaction"]
    b = pay(st, simulate="timeout", sender="A1-CUST-0003").json()["transaction"]
    for t in (a, b):
        st.ledger.apply_evidence(
            t["transaction_id"], evidence(t["transaction_id"], SwitchStatus.FAILED), actor="S"
        )
    va = st.ledger.eligibility(a["transaction_id"], "REVERSAL").current_version
    vb = st.ledger.eligibility(b["transaction_id"], "REVERSAL").current_version
    st.ledger.reverse(a["transaction_id"], "OP-SHARED", expected_version=va, actor="C", reason="r")
    with pytest.raises(SafetyViolation) as e:
        st.ledger.reverse(
            b["transaction_id"], "OP-SHARED", expected_version=vb, actor="C", reason="r"
        )
    assert e.value.code == ErrorCode.WRONG_TRANSACTION


def test_stale_version_rejected(stack_factory):
    st = stack_factory(auto_reverse_on_confirmed_failure=False)
    txn = pay(st, simulate="timeout").json()["transaction"]
    st.ledger.apply_evidence(
        txn["transaction_id"], evidence(txn["transaction_id"], SwitchStatus.FAILED), actor="S"
    )
    with pytest.raises(SafetyViolation) as e:
        st.ledger.reverse(
            txn["transaction_id"], "OP-1", expected_version=txn["version"], actor="C", reason="r"
        )
    assert e.value.code == ErrorCode.STALE_VERSION


def test_verify_ledger_detects_tampering(stack):
    pay(stack)
    assert stack.ledger.verify_ledger()["ok"]
    with stack.db.session() as s:
        from sqlalchemy import text

        s.execute(
            text(
                "UPDATE accounts SET balance_paise = balance_paise + 1 WHERE account_id='B-CUST-0001'"
            )
        )
    result = stack.ledger.verify_ledger()
    assert not result["ok"] and "B-CUST-0001" in result["balance_mismatches"]


def test_customer_balance_cannot_go_negative_at_db_level(stack):
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        with stack.db.session() as s:
            s.execute(
                text("UPDATE accounts SET balance_paise = -1 WHERE account_id='A1-CUST-0001'")
            )


def test_quarantine_blocks_automated_evidence(stack):
    txn = pay(stack, simulate="timeout").json()["transaction"]
    stack.ledger.quarantine(
        txn["transaction_id"], "conflicting evidence under review", actor="SECURITY"
    )
    with pytest.raises(SafetyViolation):
        stack.ledger.apply_evidence(
            txn["transaction_id"],
            evidence(txn["transaction_id"], SwitchStatus.SUCCESS),
            actor="SWITCH",
        )
    assert (
        stack.ledger.eligibility(txn["transaction_id"], "STATUS_CHECK").current_state
        == "QUARANTINED"
    )
