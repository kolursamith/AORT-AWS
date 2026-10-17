"""C. Flooding tests: admission control, Tier 2 credits, route isolation."""

from __future__ import annotations

from backend.aorts.contracts.enums import ErrorCode
from backend.aorts.contracts.requests import DefenseConfig
from backend.aorts.security.defense_policy import AdmissionController
from backend.aorts.security.flooding_simulator import FloodConfig, run_flood
from tests.trackb.conftest import count, pay


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def controller(clock, **kw):
    args = dict(
        bank_rate=5,
        bank_burst=5,
        route_rate=100,
        route_burst=100,
        global_rate=100,
        global_burst=100,
        credit_cap=1000,
        credit_refill=100,
    )
    args.update(kw)
    return AdmissionController(clock=clock, **args)


def test_per_bank_quota_enforced_and_other_bank_unaffected():
    clock = FakeClock()
    ac = controller(clock)
    a3 = [ac.admit("A3", "A3", "A3-B") for _ in range(20)]
    assert sum(d.admitted for d in a3) == 5
    assert all(d.code == ErrorCode.RATE_LIMITED for d in a3[5:])
    assert all(ac.admit("A1", "A1", "A1-B").admitted for _ in range(5))  # A1 keeps its own quota
    clock.t += 1.0
    assert sum(ac.admit("A3", "A3", "A3-B").admitted for _ in range(10)) == 5  # refills at 5/s


def test_claimed_bank_id_cannot_evade_quota():
    ac = controller(FakeClock())
    d = ac.admit("A3", "A1", "A1-B")
    assert not d.admitted and d.code == ErrorCode.IDENTITY_MISMATCH
    d = ac.admit("A3", "A3", "A1-B")  # route of another bank
    assert not d.admitted and d.code == ErrorCode.IDENTITY_MISMATCH


def test_credit_exhaustion_and_rejections_consume_nothing():
    clock = FakeClock()
    ac = controller(clock, bank_rate=1000, bank_burst=1000, credit_cap=3, credit_refill=1)
    results = [ac.admit("A2", "A2", "A2-C") for _ in range(6)]
    assert [r.admitted for r in results] == [True, True, True, False, False, False]
    assert results[-1].code == ErrorCode.CREDIT_EXHAUSTED and results[-1].retry_after > 0
    # Rejected requests did not drain the bank bucket.
    assert ac.quota_view("A2")["bank_tokens"] == 997
    clock.t += 1.0
    assert ac.admit("A2", "A2", "A2-C").admitted


def test_route_limit_isolates_routes():
    clock = FakeClock()
    ac = controller(clock, bank_rate=1000, bank_burst=1000, route_rate=2, route_burst=2)
    assert sum(ac.admit("A1", "A1", "A1-B").admitted for _ in range(5)) == 2
    assert (
        sum(ac.admit("A1", "A1", "A1-C").admitted for _ in range(5)) == 2
    )  # healthy route C unaffected


def test_global_ceiling():
    ac = controller(FakeClock(), bank_rate=1000, bank_burst=1000, global_rate=3, global_burst=3)
    decisions = [ac.admit(b, b, f"{b}-B") for b in ("A1", "A2", "A3", "A1")]
    assert [d.admitted for d in decisions] == [True, True, True, False]
    assert decisions[-1].code == ErrorCode.GLOBAL_CAPACITY_EXCEEDED


def test_tier2_allocation_proportional_with_floor():
    ac = controller(FakeClock())
    alloc = ac.credits.allocate(10.0, {"A1": 30, "A2": 10, "A3": 0})
    assert alloc["A1"] > alloc["A2"] > alloc["A3"] > 0
    assert abs(sum(alloc.values()) - 10.0) < 0.01  # values are rounded to 3 dp


def _txn_pool(stack, banks=("A1", "A2", "A3"), per_bank=3):
    pool = {}
    for bank in banks:
        pool[bank] = []
        for i in range(per_bank):
            txn = pay(stack, sender=f"{bank}-CUST-000{i + 1}", simulate="lost-response").json()[
                "transaction"
            ]
            pool[bank].append((txn["transaction_id"], txn["route_id"]))
    return pool


def test_flood_rejected_at_switch_before_downstream_work(stack_factory):
    st = stack_factory(
        bank_check_rate_per_sec=5,
        bank_check_burst=5,
        tier2_credit_cap=1000,
        tier2_credit_refill_per_sec=100,
    )
    pool = _txn_pool(st)
    downstream_before = st.switch_service.ctx.metrics.counters.copy()
    result = run_flood(
        st.client,
        pool,
        FloodConfig(
            attack_rps=60, duration_s=2.0, legit_rps_per_bank=2, tail_s=0.5, concurrency=32
        ),
    )
    summary = result.summary(bank_limit=5)
    a3, a1 = summary["per_bank"]["A3"], summary["per_bank"]["A1"]
    assert a3["sent"] >= 100
    assert a3["outcomes"].get("RATE_LIMITED", 0) > 0.7 * a3["sent"]
    assert a3["admitted_ok"] <= 5 + 5 * 2.6  # burst + rate x duration (+ small timing slack)
    assert a1["admitted_ratio"] == 1.0
    assert summary["attack_detection_s"] is not None and summary["attack_detection_s"] < 1.0
    counters = st.switch_service.ctx.metrics.counters
    admitted_a3 = counters.get("check_admitted{bank=A3}", 0)
    # Rejected requests never reached the switch core / downstream: completed == admitted.
    assert counters.get("check_completed{bank=A3}", 0) == admitted_a3
    assert counters.get("check_forwarded{destination=B}", 0) == downstream_before.get(
        "check_forwarded{destination=B}", 0
    )


def test_flood_with_defenses_disabled_admits_attacker(stack_factory):
    st = stack_factory(bank_check_rate_per_sec=5, bank_check_burst=5)
    st.set_defenses(DefenseConfig(rate_limiting=False, tier2_credits=False))
    pool = _txn_pool(st)
    summary = run_flood(
        st.client,
        pool,
        FloodConfig(attack_rps=40, duration_s=1.5, legit_rps_per_bank=2, tail_s=0.2),
    ).summary(bank_limit=5)
    assert summary["per_bank"]["A3"]["outcomes"].get("RATE_LIMITED", 0) == 0
    assert summary["per_bank"]["A3"]["admitted_ok"] > 20


def test_spoofed_bank_id_rejected(stack):
    pool = _txn_pool(stack, banks=("A1", "A3"), per_bank=1)
    summary = run_flood(
        stack.client,
        pool,
        FloodConfig(
            attack_rps=10,
            duration_s=0.5,
            legit_rps_per_bank=1,
            legit_banks=("A1",),
            spoof_bank_id=True,
            tail_s=0,
        ),
    ).summary()
    assert summary["per_bank"]["A3"]["outcomes"] == {
        "IDENTITY_MISMATCH": summary["per_bank"]["A3"]["sent"]
    }
    assert count(stack.db, "SELECT count(*) FROM transactions") == 2
