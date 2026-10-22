"""Stage 05: baseline policy algorithms (pure; no simulator)."""

from pathlib import Path

import pytest

from intelligence.experiments import baselines as b
from intelligence.experiments.baselines import (
    CheckOutcome,
    PolicyAction,
    PolicyCategory,
    PolicyInput,
    TxnView,
)
from intelligence.simulation.scenarios import research_timing_policy

TIMING = research_timing_policy()
REPO = Path(__file__).resolve().parents[2]


def view(txn, sent=0.0, ambiguous=15.0, k=0, last=None, route="A1-B", in_flight=False):
    return TxnView(txn, route, route.split("-")[0], sent, ambiguous, k, last, in_flight, None)


def inp(now, outstanding=(), eligible=None, feedback=(), routes=("A1-B",), k=0):
    return PolicyInput(now, k, routes, tuple(outstanding),
                       tuple(outstanding if eligible is None else eligible), tuple(feedback),
                       TIMING)  # fmt: skip


# ----------------------------------------------------------------------- algorithms


def test_no_check_never_requests():
    assert b.NoCheck().decide(inp(500.0, [view("T1")])) == PolicyAction()


def test_naive_requests_every_outstanding_transaction_oldest_first():
    out = [view("T2", ambiguous=20.0), view("T1", ambiguous=15.0), view("T3", in_flight=True)]
    action = b.NaivePolling().decide(inp(16.0, out, eligible=[]))
    assert action.transaction_ids == ("T1", "T3", "T2")  # ignores eligibility: stress only
    assert b.NaivePolling().info.category is PolicyCategory.STRESS_UNCONSTRAINED


@pytest.mark.parametrize(
    ("now", "k", "requested"),
    [(89.999, 0, False), (90.0, 0, True), (179.999, 1, False), (180.0, 1, True), (900.0, 3, False)],
)
def test_static_research_schedule_boundaries(now, k, requested):
    action = b.StaticPolling().decide(inp(now, [view("T1", sent=0.0, k=k)]))
    assert (action.transaction_ids == ("T1",)) is requested


def test_static_trackb_schedule_counts_from_ambiguity():
    p = b.trackb_static()
    assert p.decide(inp(15.999, [view("T1", ambiguous=15.0)])).transaction_ids == ()
    assert p.decide(inp(16.0, [view("T1", ambiguous=15.0)])).transaction_ids == ("T1",)
    assert p.decide(inp(17.999, [view("T1", ambiguous=15.0, k=1)])).transaction_ids == ()
    assert p.decide(inp(18.0, [view("T1", ambiguous=15.0, k=1)])).transaction_ids == ("T1",)


def test_backoff_requests_then_waits_for_its_jittered_due_time():
    p = b.ExponentialBackoffJitter()
    assert p.decide(inp(20.0, [view("T1", ambiguous=15.0)])).transaction_ids == ("T1",)
    draw = b.jitter("exp-backoff-jitter", 42, "T1", 0, "backoff")
    due_age = 5.0 + draw * min(720.0, 90.0 * 2**0)
    assert p.state()["next_due"] == {"T1": pytest.approx(due_age)}
    before = 15.0 + due_age - 1e-6
    assert p.decide(inp(before, [view("T1", ambiguous=15.0, k=1)])).transaction_ids == ()
    at_due = p.decide(inp(15.0 + due_age, [view("T1", ambiguous=15.0, k=1)]))
    assert at_due.transaction_ids == ("T1",)
    window_k1 = min(720.0, 90.0 * 2**1)
    expected = due_age + b.jitter("exp-backoff-jitter", 42, "T1", 1, "backoff") * window_k1
    assert p.state()["next_due"]["T1"] == pytest.approx(expected)


def test_fixed_budget_ranks_eligible_oldest_first_with_a_per_route_budget():
    eligible = [view("T2", ambiguous=30.0), view("T1", ambiguous=15.0)]
    action = b.FixedBudget().decide(inp(200.0, eligible, routes=("A1-B", "A2-C")))
    assert action.transaction_ids == ("T1", "T2")
    assert action.budget("A1-B") == 3 and action.budget("A2-C") == 3


def _fb(kind, detail=None, latency=None, route="A1-B"):
    return CheckOutcome("T1", route, kind, 0.0, detail, latency)


def test_aimd_increases_when_healthy_and_halves_on_any_bad_signal():
    p = b.Aimd()
    assert p.decide(inp(1.0)).budget("A1-B") == 3  # 2 + 1, no feedback is healthy
    healthy = [_fb("RELEASED"), _fb("ANSWERED", "AUTHORITATIVE_SUCCESS", 0.2)]
    assert p.decide(inp(2.0, feedback=healthy)).budget("A1-B") == 4
    bad_signals = (
        _fb("REFUSED", "rate_limited"),
        _fb("TIMED_OUT", None, 10.0),
        _fb("ANSWERED", "PENDING", 0.2),
        _fb("ANSWERED", "AUTHORITATIVE_SUCCESS", 0.6),
    )
    for bad in bad_signals:
        q = b.Aimd(budget={"A1-B": 8.0})
        assert q.decide(inp(3.0, feedback=[bad])).budget("A1-B") == 4
    floor = b.Aimd(budget={"A1-B": 1.0})
    assert floor.decide(inp(3.0, feedback=[_fb("REFUSED")])).budget("A1-B") == 1
    ceiling = b.Aimd(budget={"A1-B": 50.0})
    assert ceiling.decide(inp(3.0)).budget("A1-B") == 50


def test_aimd_routes_are_independent():
    p = b.Aimd()
    action = p.decide(inp(1.0, feedback=[_fb("REFUSED", route="A2-C")], routes=("A1-B", "A2-C")))
    assert (action.budget("A1-B"), action.budget("A2-C")) == (3, 1)


# --------------------------------------------------------------- jitter and state


def test_jitter_is_deterministic_and_isolated():
    a = b.jitter("p", 1, "T1", 0, "backoff")
    assert a == b.jitter("p", 1, "T1", 0, "backoff") and 0.0 <= a < 1.0
    others = {b.jitter("q", 1, "T1", 0, "backoff"), b.jitter("p", 2, "T1", 0, "backoff"),
              b.jitter("p", 1, "T2", 0, "backoff"), b.jitter("p", 1, "T1", 1, "backoff"),
              b.jitter("p", 1, "T1", 0, "other")}  # fmt: skip
    assert a not in others and len(others) == 5


def test_backoff_is_independent_of_order_and_of_other_transactions():
    one = b.ExponentialBackoffJitter()
    two = b.ExponentialBackoffJitter()
    one.decide(inp(20.0, [view("T1"), view("T2"), view("T3")]))
    two.decide(inp(20.0, [view("T3"), view("T1")]))
    assert one.state()["next_due"]["T1"] == two.state()["next_due"]["T1"]
    assert one.state()["next_due"]["T3"] == two.state()["next_due"]["T3"]


def test_policy_state_is_not_shared_between_instances():
    a, c = b.Aimd(), b.Aimd()
    a.decide(inp(1.0))
    assert c.state() == {"budget": {}}
    x, y = b.ExponentialBackoffJitter(), b.ExponentialBackoffJitter()
    x.decide(inp(20.0, [view("T1")]))
    assert y.state() == {"next_due": {}}


# ------------------------------------------------------------------------ registry


def test_registry_categories_and_not_comparable_entries():
    for pid in b.BASELINES:
        policy = b.make_policy(pid)
        assert policy.info.policy_id == pid and policy.info.version
    assert b.make_policy("no-check").info.category is PolicyCategory.REFERENCE
    strict = {"static-polling", "exp-backoff-jitter", "fixed-budget", "aimd",
              "static-polling-trackb", "exp-backoff-jitter-trackb"}  # fmt: skip
    assert {p for p in b.BASELINES if b.make_policy(p).info.category is PolicyCategory.STRICT} == (
        strict
    )
    with pytest.raises(ValueError, match="switch-measured"):
        b.make_policy("trackb-heuristic")
    with pytest.raises(ValueError, match="does not exist yet"):
        b.make_policy("aorts")
    assert "bbr" not in " ".join(b.BASELINES).lower()


def test_policies_cannot_reach_clocks_truth_or_money():
    import ast

    tree = ast.parse((REPO / "intelligence/experiments/baselines.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert imported <= {"hashlib", "random", "collections.abc", "dataclasses", "enum", "typing",
                        "contracts.v1_1"}, imported  # fmt: skip
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    for forbidden in ("now", "time", "ground_truth", "capacity_rps", "credited", "would_repay"):
        assert forbidden not in names, forbidden
