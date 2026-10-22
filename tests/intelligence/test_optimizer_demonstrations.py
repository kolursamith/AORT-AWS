"""Stage 08: the executed demonstrations run end to end (one DES case and the deadline case)."""

import pytest

pytest.importorskip("simpy")

from intelligence.experiments import lookahead_evaluation as le  # noqa: E402
from intelligence.experiments import optimizer_demonstrations as od  # noqa: E402


def test_deadline_demonstration_prefers_escalated_transactions():
    demo = od.deadline_demo()
    assert demo["full"]["reason"] == "SAFE_BUDGET_SELECTED"
    assert set(demo["full"]["first_selected"][:5]) == set(demo["escalated_ids"])
    assert not set(demo["no_deadline_ranking"]["first_selected"]) & set(demo["escalated_ids"])


def test_congested_route_demonstration_chooses_zero_and_measures_the_alternative():
    case = next(c for c in le.cases(od.SEED) if c.name == "prolonged_capacity_loss")
    demo = od.des_demo("congested", case)
    assert demo["full"]["budget_rps"] == 0 and demo["full"]["mode"] == "NORMAL"
    zero, fixed = demo["actual_next_60s"][0], demo["actual_next_60s"][-1]
    assert zero["budget_rps"] == 0 and fixed["budget_rps"] == od.FIXED_REFERENCE_RPS
    assert fixed["queue_mean"] > zero["queue_mean"]  # the reference budget only adds load
    assert fixed["resolved_by_check"] == 0
