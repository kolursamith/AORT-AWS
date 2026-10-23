"""PreDispatchGate branches and deterministic keyed fault injection."""

from __future__ import annotations

from types import SimpleNamespace

from backend.aorts.banking.faults.fault_service import FaultInjector
from backend.aorts.contracts.requests import FaultConfig
from backend.aorts.recovery.gates import PreDispatchGate


class FakeLedger:
    def __init__(self, eligible=True, reason="state CLOSED is not AMBIGUOUS"):
        self.eligible, self.reason = eligible, reason

    def eligibility(self, txn_id, action, version):
        return SimpleNamespace(
            eligible=self.eligible, reasons=[] if self.eligible else [self.reason]
        )


TXN = SimpleNamespace(transaction_id="T1", version=2, route_id="A1-B")
OK_MEAS = {"workers": 8, "in_flight": 1, "backlog": 0}


def check(gate, **kw):
    args = dict(dispatched_on_route=0, route_budget=3, inflight_on_route=0, measured=OK_MEAS)
    args.update(kw)
    return gate.check(TXN, **args)


def test_gate_allows_when_all_checks_pass():
    g = PreDispatchGate(FakeLedger())
    assert check(g).allowed and g.stats["allowed"] == 1


def test_gate_branches():
    g = PreDispatchGate(FakeLedger(), max_inflight_per_route=4, primary_reserve_fraction=0.25)
    assert check(g, dispatched_on_route=3).reason == "route_budget_exhausted"
    assert check(g, inflight_on_route=4).reason == "route_inflight_limit"
    assert (
        check(g, measured={"workers": 8, "in_flight": 5, "backlog": 1}).reason
        == "primary_capacity_reserved"
    )
    assert check(PreDispatchGate(FakeLedger(False))).reason.startswith("ineligible")
    assert set(g.stats) == {
        "route_budget_exhausted",
        "route_inflight_limit",
        "primary_capacity_reserved",
    }


class FakeStore:
    def __init__(self, cfg):
        self.cfg = cfg

    def fault(self, target):
        return self.cfg if target == self.cfg.target else None


def test_keyed_fault_decisions_are_deterministic_and_order_independent():
    cfg = FaultConfig(target="beneficiary:B", error_rate=0.5, drop_response_rate=0.4)
    keys = [f"B-CUST-000{i % 9}:{1000 + i}" for i in range(200)]
    a = FaultInjector(FakeStore(cfg), seed=7)
    b = FaultInjector(FakeStore(cfg), seed=7)
    first = {k: a.decide("beneficiary:B", stream="credit", key=k) for k in keys}
    for _ in range(500):  # unrelated status-check traffic on another stream
        b.decide("beneficiary:B", stream="query")
    second = {k: b.decide("beneficiary:B", stream="credit", key=k) for k in reversed(keys)}
    assert all(
        (first[k].error, first[k].drop_response) == (second[k].error, second[k].drop_response)
        for k in keys
    )
    rate = sum(d.error for d in first.values()) / len(keys)
    assert 0.35 < rate < 0.65  # honours the configured probability
    c = FaultInjector(FakeStore(cfg), seed=8)
    assert any(
        c.decide("beneficiary:B", stream="credit", key=k).error != first[k].error for k in keys
    )
