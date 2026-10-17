"""Reference recovery policies behave as documented."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from backend.aorts.adapters.baseline_policies import (
    Aimd,
    ExponentialBackoffJitter,
    FixedBudget,
    NaivePolling,
    StaticSchedule,
)
from backend.aorts.adapters.controller_client import make_controller
from backend.aorts.app.config import Settings
from backend.aorts.contracts.requests import RecoveryDecisionRequest

NOW = datetime(2026, 10, 16, 10, 0, tzinfo=UTC)


def req(cands, routes=("A1-B",)):
    return RecoveryDecisionRequest(
        decision_request_id="d",
        timestamp=NOW,
        routes=[{"route_id": r} for r in routes],
        candidates=cands,
    )


def cand(i, age_s, checks=0, route="A1-B"):
    return {
        "transaction_id": f"T{i}",
        "route_id": route,
        "checks_used": checks,
        "ambiguous_since": (NOW - timedelta(seconds=age_s)).isoformat(),
    }


def test_naive_polling_checks_everything():
    d = NaivePolling().decide(req([cand(i, 1) for i in range(5)]))
    assert len(d.transaction_ids) == 5 and d.route_budgets["A1-B"] == 5


def test_static_schedule_respects_check_times():
    d = StaticSchedule(schedule_s=(2.0, 5.0)).decide(
        req(
            [
                cand(1, 1.0),
                cand(2, 2.5),
                cand(3, 4.0, checks=1),
                cand(4, 6.0, checks=1),
                cand(5, 99, checks=2),
            ]
        )
    )
    assert d.transaction_ids == ["T2", "T4"]  # T5 has exhausted the schedule


def test_backoff_spaces_out_rechecks():
    p = ExponentialBackoffJitter(base_s=1.0, seed=1)
    first = p.decide(req([cand(1, 0.0)]))
    assert first.transaction_ids == ["T1"]
    again = p.decide(req([cand(1, 0.0, checks=3)]))  # same age: not due yet
    assert again.transaction_ids == []


def test_fixed_budget_is_constant_and_oldest_first():
    d = FixedBudget(per_route=2).decide(req([cand(1, 1), cand(2, 9), cand(3, 5)]))
    assert d.route_budgets == {"A1-B": 2} and d.transaction_ids[:2] == ["T2", "T3"]


def test_aimd_increases_when_healthy_and_halves_on_trouble():
    p = Aimd(start=4)
    assert p.decide(req([])).route_budgets["A1-B"] == 5
    p.observe({"route_id": "A1-B", "admitted": True, "status": "SUCCESS", "latency_ms": 50})
    assert p.decide(req([])).route_budgets["A1-B"] == 6
    p.observe(
        {"route_id": "A1-B", "admitted": False, "status": None, "rejection_reason": "RATE_LIMITED"}
    )
    assert p.decide(req([])).route_budgets["A1-B"] == 3


def test_controller_mode_baseline_is_selectable():
    c = make_controller(Settings(_env_file=None, controller_mode="baseline:aimd"))
    assert c.health()["policy"] == "aimd"
