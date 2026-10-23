"""AORTS recovery intelligence: capacity estimation, budget optimization, scheduling."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from backend.aorts.recovery.budget import BudgetConfig, BudgetOptimizer
from backend.aorts.recovery.capacity import CapacityEstimator
from backend.aorts.recovery.scheduler import PriorityScheduler
from backend.aorts.twin.route_twin import RouteTwinStore
from backend.aorts.twin.transaction_twin import TransactionTwinState

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
MEAS = {
    "workers": 8,
    "latency_p50_ms": 100.0,
    "latency_p95_ms": 160.0,
    "throughput_tps": 20.0,
    "in_flight": 2,
    "backlog": 0,
    "samples": 40,
}


def route(reliability_verdicts=("TRUSTED",) * 5, measured=MEAS, history=(80.0,) * 5):
    rs = RouteTwinStore()
    rt = rs.get("A1-B")
    for v in reliability_verdicts:
        rs.on_telemetry("A1-B", v, {"throughput_tps": 20.0})
    if measured:
        rt.measured = dict(measured)
        rt.measured_capacity_history.extend(history)
    return rt


def test_capacity_from_independent_measurement_with_confidence():
    est = CapacityEstimator().estimate(route())
    assert est.point_rps == 80.0 and 0.9 < est.confidence <= 1.0 and est.low_rps <= est.point_rps


def test_false_high_reported_capacity_does_not_raise_estimate():
    honest = CapacityEstimator().estimate(route())
    rt = route()
    rt.last_reported = {"throughput_tps": 10_000.0}  # even a TRUSTED-looking wild claim
    assert CapacityEstimator().estimate(rt).point_rps == honest.point_rps


def test_low_reliability_shrinks_lower_bound():
    good = CapacityEstimator().estimate(route())
    bad = CapacityEstimator().estimate(route(("REJECTED",) * 5))
    assert bad.confidence < good.confidence and bad.low_rps < good.low_rps


def test_no_measurement_means_no_capacity():
    est = CapacityEstimator().estimate(route(measured=None))
    assert est.low_rps is None and est.confidence == 0.0


def decide(est, **kw):
    args = dict(
        primary_tps=10.0, backlog=20, p50_ms=100.0, p_resolve=0.5, min_time_to_deadline_s=60.0
    )
    args.update(kw)
    return BudgetOptimizer(BudgetConfig(tick_s=1.0)).decide(est, **args)


def test_optimizer_evaluates_ladder_and_reserves_primary():
    est = CapacityEstimator().estimate(route())
    d = decide(est)
    assert len(d.candidates) >= 5 and d.mode == "optimized" and d.budget > 0
    reserved = 10.0 * 1.25
    assert reserved + d.budget <= 0.75 * est.low_rps  # never eats the primary reservation
    assert all(
        not c["feasible"] for c in d.candidates if reserved + c["budget"] > 0.75 * est.low_rps
    )
    assert any("primary load" in r for r in d.reasons)


def test_optimizer_prefers_smallest_budget_meeting_target():
    est = CapacityEstimator().estimate(route())
    d = decide(est, backlog=4)
    feasible_meeting = [
        c
        for c in d.candidates
        if c["feasible"]
        and c["budget"] > 0
        and c["pred_clearance_s"] <= d.predicted["target_clearance_s"]
    ]
    assert d.budget == feasible_meeting[0]["budget"]


def test_no_spare_capacity_means_zero_unless_deadline_imminent():
    est = CapacityEstimator().estimate(route())
    assert decide(est, primary_tps=60.0).budget == 0
    assert decide(est, primary_tps=60.0, min_time_to_deadline_s=1.0).budget <= 1


def test_unreliable_telemetry_forces_conservative_fallback():
    trusted = decide(CapacityEstimator().estimate(route()))
    poisoned = decide(CapacityEstimator().estimate(route(("REJECTED",) * 5)))
    assert (
        poisoned.mode == "fallback" and poisoned.budget <= 2 and poisoned.budget <= trusted.budget
    )


def test_no_backlog_no_budget():
    assert decide(CapacityEstimator().estimate(route()), backlog=0).budget == 0


def cand(i, age, deadline_in, checks=0):
    return {
        "transaction_id": f"T{i}",
        "route_id": "A1-B",
        "checks_used": checks,
        "ambiguous_since": (NOW - timedelta(seconds=age)).isoformat(),
        "deadline_at": (NOW + timedelta(seconds=deadline_in)).isoformat(),
    }


def test_scheduler_prioritises_urgency_and_explains():
    ranked = PriorityScheduler(starvation_s=1e9).rank(
        [cand(1, 5, 100), cand(2, 5, 5), cand(3, 50, 60)], {}, NOW
    )
    assert ranked[0].transaction_id == "T2"  # nearest deadline
    assert set(ranked[0].components) >= {"urgency", "age", "evidence", "retry"}


def test_scheduler_penalises_retries_and_inconclusive_evidence():
    twins = {"T1": TransactionTwinState("T1", checks_started=4, inconclusive_checks=4)}
    ranked = PriorityScheduler(starvation_s=1e9).rank(
        [cand(1, 10, 60, checks=4), cand(2, 10, 60)], twins, NOW
    )
    assert ranked[0].transaction_id == "T2"


def test_starvation_guard_forces_long_waiting_transactions():
    twins = {
        "T1": TransactionTwinState(
            "T1", checks_started=9, inconclusive_checks=9, last_check_at=NOW - timedelta(seconds=30)
        )
    }
    ranked = PriorityScheduler(starvation_s=8).rank(
        [cand(1, 40, 80, checks=9), cand(2, 1, 5)], twins, NOW
    )
    assert ranked[0].transaction_id == "T1" and ranked[0].forced
