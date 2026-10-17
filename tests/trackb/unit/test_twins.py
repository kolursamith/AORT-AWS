"""Route/Transaction Twin event handling: duplicates, stale, delayed, out-of-order, reconstruction."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from backend.aorts.twin.engine import TwinEngine

T0 = datetime(2026, 10, 17, 10, 0, tzinfo=UTC)


def ev(
    eid,
    version,
    state,
    etype="CHECK_RESULT",
    txn="T1",
    route="A1-B",
    outcome="UNKNOWN",
    at=0,
    **payload,
):
    return {
        "event_id": eid,
        "transaction_id": txn,
        "route_id": route,
        "bank_id": "A1",
        "amount_paise": 1000,
        "event_type": etype,
        "transaction_version": version,
        "timestamp": (T0 + timedelta(seconds=at)).isoformat(),
        "payload": {"last_known_state": state, "financial_outcome": outcome, **payload},
    }


LIFECYCLE = [
    ev(
        "e1", 1, "DEBITED", "PAYMENT_DEBITED", deadline_at=(T0 + timedelta(seconds=120)).isoformat()
    ),
    ev("e2", 2, "AMBIGUOUS", "PAYMENT_AMBIGUOUS", at=1),
    ev("e3", 3, "CHECK_IN_FLIGHT", "CHECK_STARTED", at=2),
    ev("e4", 4, "AMBIGUOUS", "CHECK_RESULT", at=3),
    ev("e5", 5, "CHECK_IN_FLIGHT", "CHECK_STARTED", at=4),
    ev(
        "e6",
        6,
        "CONFIRMED_SUCCESS",
        "PAYMENT_SUCCEEDED",
        outcome="SUCCESS",
        at=5,
        evidence_source="SWITCH",
    ),
    ev("e7", 7, "CLOSED", "PAYMENT_CLOSED", outcome="SUCCESS", at=5),
]


def test_in_order_lifecycle():
    e = TwinEngine()
    for x in LIFECYCLE:
        assert e.apply_event(x) == "applied"
    t = e.txns.twins["T1"]
    assert (t.state, t.version, t.financial_outcome) == ("CLOSED", 7, "SUCCESS")
    assert (
        t.checks_started == 2 and t.inconclusive_checks == 1 and t.evidence[0]["source"] == "SWITCH"
    )
    assert t.deadline_at == T0 + timedelta(seconds=120) and not t.unresolved
    assert len(e.routes.get("A1-B").primary_events) == 1


def test_duplicates_are_ignored():
    e = TwinEngine()
    for x in LIFECYCLE + LIFECYCLE:
        e.apply_event(x)
    assert e.txns.stats["duplicates"] == len(LIFECYCLE)
    assert (
        len(e.routes.get("A1-B").primary_events) == 1
    )  # duplicate debit does not inflate primary load


def test_stale_and_delayed_events_do_not_regress_state():
    e = TwinEngine()
    for x in LIFECYCLE:
        e.apply_event(x)
    late = ev("late-1", 4, "AMBIGUOUS", "CHECK_RESULT", at=99)  # delayed old version, new id
    assert e.apply_event(late) == "stale"
    assert e.txns.twins["T1"].state == "CLOSED"


def test_out_of_order_converges_to_same_state():
    in_order = TwinEngine()
    for x in LIFECYCLE:
        in_order.apply_event(x)
    for seed in range(20):
        shuffled = list(LIFECYCLE)
        random.Random(seed).shuffle(shuffled)
        e = TwinEngine()
        for x in shuffled:
            e.apply_event(x)
        t = e.txns.twins["T1"]
        assert (t.state, t.version, t.financial_outcome) == ("CLOSED", 7, "SUCCESS")
        assert not t.unresolved


def test_conflicting_same_version_event_ignored():
    e = TwinEngine()
    e.apply_event(ev("a", 2, "AMBIGUOUS"))
    assert e.apply_event(ev("b", 2, "CONFIRMED_FAILED", outcome="FAILURE")) == "conflict"
    assert e.txns.twins["T1"].state == "AMBIGUOUS"


def test_route_twin_telemetry_reliability_and_backlog():
    e = TwinEngine()
    for v in ("TRUSTED", "TRUSTED", "REJECTED", "SUSPICIOUS"):
        e.apply_telemetry("A1-B", v, {"throughput_tps": 999.0})
    rt = e.routes.get("A1-B")
    assert 0.0 < rt.telemetry_reliability < 1.0 and rt.last_verdict == "SUSPICIOUS"
    e.apply_event(ev("x1", 1, "DEBITED", "PAYMENT_DEBITED", txn="T9"))
    e.apply_event(ev("x2", 2, "AMBIGUOUS", "PAYMENT_AMBIGUOUS", txn="T9"))
    snap = e.snapshot(now=T0)
    assert snap["routes"]["A1-B"]["recovery_backlog"] == 1 and snap["unresolved_count"] == 1
