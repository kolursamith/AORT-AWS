"""Bulkhead: a status-check flood that bypasses admission control must not starve payment processing."""

from __future__ import annotations

import threading

from backend.aorts.contracts.requests import DefenseConfig
from backend.aorts.security.flooding_simulator import FloodConfig, run_flood
from tests.trackb.conftest import pay


def _flood_with_payments(st, n_payments=12):
    st.set_defenses(DefenseConfig(rate_limiting=False, tier2_credits=False))
    pool = {}
    for bank in ("A3", "A1"):
        t = pay(st, sender=f"{bank}-CUST-0009", simulate="lost-response").json()["transaction"]
        pool[bank] = [(t["transaction_id"], t["route_id"])]
    results = []

    def payments():
        for i in range(n_payments):
            r = pay(
                st,
                sender=f"A2-CUST-000{(i % 8) + 1}",
                beneficiary="C-CUST-0001",
                bank="C",
                amount=1000,
            )
            results.append(r.json()["transaction"]["state"])

    th = threading.Thread(target=payments)
    th.start()
    run_flood(
        st.client,
        pool,
        FloodConfig(
            attack_rps=150,
            duration_s=2.0,
            legit_banks=("A1",),
            legit_rps_per_bank=1,
            tail_s=0,
            concurrency=64,
        ),
    )
    th.join()
    return results


TIGHT = dict(switch_core_workers=2, switch_base_processing_ms=40, switch_queue_wait_s=0.05)


def test_shared_pool_lets_undefended_flood_hurt_payments(stack_factory):
    # Counterfactual: no bulkhead AND no primary-capacity reservation.
    st = stack_factory(**TIGHT, switch_check_workers=0, switch_primary_reserve_fraction=0.0)
    states = _flood_with_payments(st)
    overloaded = st.switch_service.ctx.metrics.counters.get("core_overloaded{kind=pay}", 0)
    # Shared pool: payments compete with the flood; some are refused by the switch (and safely reversed).
    assert overloaded > 0 and states.count("REVERSED") == overloaded
    assert st.ledger.verify_ledger()["ok"]


def test_bulkhead_keeps_payments_flowing_during_undefended_flood(stack_factory):
    st = stack_factory(**TIGHT, switch_check_workers=2)
    states = _flood_with_payments(st)
    assert st.switch_service.ctx.metrics.counters.get("core_overloaded{kind=pay}", 0) == 0
    assert states.count("CLOSED") == len(states)
    # The flood still hurts *status checks* (that is what admission control is for).
    assert st.switch_service.ctx.metrics.counters.get("core_overloaded{bank=A3,kind=check}", 0) > 0


def test_primary_reserve_protects_payments_even_on_a_shared_pool(stack_factory):
    st = stack_factory(**TIGHT, switch_check_workers=0, switch_primary_reserve_fraction=0.5)
    states = _flood_with_payments(st)
    assert st.switch_service.ctx.metrics.counters.get("core_overloaded{kind=pay}", 0) == 0
    assert states.count("CLOSED") == len(states)
