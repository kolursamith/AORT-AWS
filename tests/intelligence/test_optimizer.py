"""Stage 08: safe recovery-budget optimizer, transaction ranker and v1.1 decisions.

Estimates come from the real Stage 06 estimator fed through the Route Twin; pools come from the
real Transaction Twin; decisions are checked with the real v1.1 codec and execution gate.
"""

import ast
from datetime import timedelta
from pathlib import Path

import pytest

from contracts.v1_1 import (
    SCHEMA_VERSION,
    DecisionMode,
    DecisionReason,
    DecisionRequest,
    RecoveryDecision,
    RouteDescriptor,
)
from contracts.v1_1.protocol import (
    ControllerOutcome,
    DispatchCandidate,
    DispatchLimits,
    decode,
    encode,
    gate_decision,
)
from intelligence.common.clock import ManualClock
from intelligence.controller.lookahead import CheckCandidate, CheckEvidence, CheckOutcomeCounts
from intelligence.controller.optimizer import (
    PROFILES,
    Ablation,
    DecisionContext,
    OptimizerConfig,
    OptimizerInputError,
    RouteObservables,
    decide,
    objective_terms,
)
from intelligence.controller.ranker import (
    PoolEntry,
    pool_from_twin,
    rank_pool,
    select_for_dispatch,
)
from intelligence.ml.capacity_estimator import EstimatorMode
from intelligence.twin.transaction_twin import TransactionTwinRegistry
from tests.intelligence.test_capacity_estimator import CONGESTED, HEALTHY, Feed
from tests.intelligence.test_transaction_twin import T0 as TWIN_T0
from tests.intelligence.test_transaction_twin import (
    answered,
    dispatched,
    initiated,
    make_policy,
    timed_out,
)

REPO = Path(__file__).resolve().parents[2]
POLICY = make_policy()
EXP, ROUTE = "EXP-CAP", "A1-B"
GOOD_OUTCOMES = CheckOutcomeCounts(authoritative=40, unknown=3, pending=2)
LA = OptimizerConfig().lookahead


def open_route():
    """A healthy route that demonstrated 45 rps and now carries 20: positive safe headroom."""

    feed = Feed()
    feed.push(120, payment_rps=30.0, throughput=30.0, p95_ms=300.0, timeouts=0, queue=4)
    feed.push(150, payment_rps=45.0, throughput=45.0, p95_ms=300.0, timeouts=0, queue=6)
    estimate = feed.push(150, payment_rps=20.0, throughput=20.0, p95_ms=300.0, timeouts=0, queue=3)
    assert estimate.safe_headroom_rps > 5
    return feed, estimate


def entry(
    i,
    now,
    *,
    eligible_s=-5.0,
    deadline_s=3000.0,
    escalated=False,
    bank="A1",
    version=3,
    checks=0,
    evidence=CheckEvidence.NO_CHECK_YET,
    age_s=200.0,
    route=ROUTE,
    exp=EXP,
):
    return PoolEntry(
        CheckCandidate(
            exp,
            route,
            f"T{i:04d}",
            version,
            now + timedelta(seconds=eligible_s),
            checks,
            now + timedelta(seconds=deadline_s),
            evidence,
            None,
            bank,
        ),
        now - timedelta(seconds=age_s),
        escalated,
    )


def context(feed, estimate, pool, *, outcomes=GOOD_OUTCOMES, **kw):
    obs = RouteObservables(
        observed_outstanding=3,
        observed_median_latency_s=0.3,
        observed_timeout_rate=0.0,
        late_resolution_hazard_per_s=0.0,
        ambiguous_backlog=len(pool),
        check_outcomes=outcomes,
    )
    base = dict(
        experiment_id=EXP,
        route_id=ROUTE,
        decision_time=feed.clock.now(),
        snapshot=feed.snapshot(),
        capacity=estimate,
        pool=tuple(pool),
        observables=obs,
        policy=POLICY,
    )
    return DecisionContext(**{**base, **kw})


def request_for(ctx):
    return DecisionRequest(
        schema_version=SCHEMA_VERSION,
        experiment_id=ctx.experiment_id,
        route_id=ctx.route_id,
        requested_at=ctx.decision_time,
    )


def gate(
    ctx,
    decision,
    *,
    versions=None,
    terminal=(),
    in_flight=(),
    now=None,
    policy=POLICY,
    rate=1000,
    credit=None,
    route=None,
):
    cands = {}
    for e in ctx.pool:
        c = e.candidate
        cands[c.transaction_id] = DispatchCandidate(
            transaction_id=c.transaction_id,
            experiment_id=c.experiment_id,
            route_id=route or c.route_id,
            state_version=(versions or {}).get(c.transaction_id, c.version),
            terminal=c.transaction_id in terminal,
            check_in_flight=c.transaction_id in in_flight,
            counted_check_attempts=c.checks_used,
            eligible_from=c.eligible_at,
            eligible_until=None,
        )
    return gate_decision(
        encode(decision),
        request=request_for(ctx),
        controller_id="aorts-controller",
        authorized_controllers=frozenset({"aorts-controller"}),
        timing_policy=policy,
        candidates=cands,
        limits=DispatchLimits(rate_allowance=rate, credit_allowance=credit),
        now=now or ctx.decision_time,
        dispatch_window_s=5.0,
        max_clock_skew_s=1.0,
    )


# ======================================================================= ranker


def test_ranker_is_deadline_aware_and_deterministic():
    _, _ = open_route()
    now = Feed().clock.now()
    pool = [
        entry(1, now, deadline_s=3000),
        entry(2, now, deadline_s=500, escalated=True),
        entry(3, now, deadline_s=1000),
        entry(4, now, deadline_s=1000, evidence=CheckEvidence.LAST_CHECK_UNKNOWN),
        entry(5, now, deadline_s=1000, checks=2),
    ]
    ranked = rank_pool(pool, 0.8, LA)
    assert [e.transaction_id for e in ranked] == ["T0002", "T0003", "T0005", "T0004", "T0001"]
    assert rank_pool(list(reversed(pool)), 0.8, LA) == ranked  # input order does not matter
    by_age = rank_pool(pool, 0.8, LA, deadline_ranking=False)
    assert by_age[0].transaction_id == "T0001"  # equal ages: eligibility, then id


def test_ranker_interleaves_banks_and_selection_honours_quota_and_eligibility():
    now = Feed().clock.now()
    pool = [entry(i, now, bank="A1", age_s=300 - i) for i in range(4)]
    pool += [entry(10 + i, now, bank="A2", age_s=100 - i) for i in range(2)]
    pool.append(entry(20, now, bank="A2", eligible_s=30.0))  # not yet eligible
    ranked = rank_pool(pool, 0.5, LA)
    banks = [e.candidate.remitter_bank_id for e in ranked]
    assert banks[:4] == ["A1", "A2", "A1", "A2"]
    chosen = select_for_dispatch(ranked, now, 10, {"A1": 1, "A2": 5})
    assert [e.candidate.remitter_bank_id for e in chosen].count("A1") == 1
    assert "T0020" not in {e.transaction_id for e in chosen}
    assert len({e.transaction_id for e in chosen}) == len(chosen)


def _twin_with_pool():
    """Five timed-out payments: TB has a check in flight, TC was answered (terminal)."""

    clock = ManualClock(TWIN_T0)
    twin = TransactionTwinRegistry(
        POLICY,
        clock,
        routes=[
            RouteDescriptor(
                schema_version=SCHEMA_VERSION,
                experiment_id=EXP,
                route_id=ROUTE,
                remitter_bank_id="A1",
                switch_id="SW",
                beneficiary_bank_id="B",
            )
        ],
    )
    clock.set_time(TWIN_T0 + timedelta(seconds=300))
    for txn in ("TA", "TB", "TC", "TD", "TE"):
        assert twin.apply_event(initiated(txn=txn, exp=EXP)).applied
        assert twin.apply_event(timed_out(txn=txn, exp=EXP)).applied
    assert twin.apply_event(dispatched(3, txn="TB", exp=EXP)).applied
    assert twin.apply_event(dispatched(3, txn="TC", exp=EXP)).applied
    assert twin.apply_event(answered(4, "AUTHORITATIVE_SUCCESS", txn="TC", exp=EXP)).applied
    return twin, clock


def test_pool_from_twin_excludes_terminal_and_in_flight_and_keeps_versions():
    twin, clock = _twin_with_pool()
    now = clock.now()
    pool = pool_from_twin(twin, EXP, ROUTE, now, {"TD": CheckEvidence.LAST_CHECK_PENDING})
    ids = {e.transaction_id for e in pool}
    assert ids == {"TA", "TD", "TE"}  # TB in flight, TC terminal
    for e in pool:
        assert e.candidate.version == twin.snapshot(EXP, e.transaction_id).version
        assert e.candidate.eligible_at <= now  # first-check delay 90 s has passed
    assert {e.transaction_id: e.candidate.evidence for e in pool}["TD"] is (
        CheckEvidence.LAST_CHECK_PENDING
    )
    assert pool_from_twin(twin, EXP, "A1-C", now) == ()
    assert pool_from_twin(twin, "EXP-OTHER", ROUTE, now) == ()


def test_pool_respects_first_check_delay():
    twin, clock = _twin_with_pool()
    early = clock.now() - timedelta(seconds=250)  # 50 s after initiation, before 90 s
    pool = pool_from_twin(twin, EXP, ROUTE, early)
    assert pool and all(e.candidate.eligible_at > early for e in pool)
    assert select_for_dispatch(rank_pool(pool, 0.5, LA), early, 10) == ()


# ======================================================================= optimizer core


def test_budget_never_exceeds_conservative_headroom_and_selection_is_eligible():
    feed, est = open_route()
    now = feed.clock.now()
    pool = [entry(i, now) for i in range(40)] + [entry(100, now, eligible_s=50.0)]
    result = decide(context(feed, est, pool))
    d = result.decision
    assert (
        d.decision_mode is DecisionMode.NORMAL
        and d.reason_code is DecisionReason.SAFE_BUDGET_SELECTED
    )
    assert 1 <= d.budget_rps <= int(est.safe_headroom_rps)
    assert len(d.selected_transaction_ids) <= d.budget_rps * 5.0
    eligible = {e.transaction_id for e in pool if e.candidate.eligible_at <= now}
    assert set(d.selected_transaction_ids) <= eligible and "T0100" not in d.selected_transaction_ids
    assert [r.version for r in d.state_versions] == [3] * len(d.selected_transaction_ids)
    budgets = {e.budget_rps for e in result.evaluations}
    assert 0 in budgets and max(budgets) == result.cap_rps == int(est.safe_headroom_rps)
    assert result.forecast is not None and result.forecast.available


def test_all_candidates_share_one_forecast_and_assumptions():
    feed, est = open_route()
    result = decide(context(feed, est, [entry(i, feed.clock.now()) for i in range(40)]))
    f = result.forecast
    assert [b.budget_rps for b in f.budgets] == sorted(e.budget_rps for e in result.evaluations)
    payments = {round(b.band(b.bands[0].band).payment_arrivals, 9) for b in f.budgets}
    assert len(payments) == 1  # identical exogenous load for every candidate


def test_decision_passes_the_real_execution_gate():
    feed, est = open_route()
    ctx = context(feed, est, [entry(i, feed.clock.now()) for i in range(40)])
    d = decide(ctx).decision
    assert decode(RecoveryDecision, encode(d)) == d
    plan = gate(ctx, d)
    assert plan.outcome is ControllerOutcome.DECISION and not plan.refused
    assert plan.admitted == d.selected_transaction_ids[: d.budget_rps * 5]


def test_gate_still_refuses_stale_terminal_in_flight_expired_wrong_route_and_policy():
    feed, est = open_route()
    ctx = context(feed, est, [entry(i, feed.clock.now()) for i in range(40)])
    d = decide(ctx).decision
    first, second, third = d.selected_transaction_ids[:3]
    plan = gate(ctx, d, versions={first: 4}, terminal={second}, in_flight={third})
    refused = dict(plan.refused)
    assert refused == {first: "STALE_VERSION", second: "TERMINAL", third: "CHECK_IN_FLIGHT"}
    expired = gate(ctx, d, now=d.expires_at)
    assert expired.outcome is ControllerOutcome.DECISION_EXPIRED and not expired.admitted
    other = make_policy(policy_id="another-policy")
    assert gate(ctx, d, policy=other).outcome is ControllerOutcome.TIMING_POLICY_MISMATCH
    wrong = gate(ctx, d, route="A1-C")
    assert {r for _, r in wrong.refused} == {"WRONG_ROUTE"} and not wrong.admitted
    capped = gate(ctx, d, credit=2)
    assert len(capped.admitted) == 2  # real credit limits are enforced by the gate


def test_zero_headroom_means_zero_budget():
    feed = Feed()
    est = feed.push(150, **HEALTHY)
    assert est.safe_headroom_rps == 0
    d = decide(context(feed, est, [entry(i, feed.clock.now()) for i in range(10)])).decision
    assert d.decision_mode is DecisionMode.NORMAL and d.reason_code is DecisionReason.NO_SAFE_BUDGET
    assert d.budget_rps == 0 and d.selected_transaction_ids == ()


def test_congested_route_gets_zero_even_with_valuable_checks():
    feed, _ = open_route()
    est = feed.push(30, **CONGESTED)
    d = decide(context(feed, est, [entry(i, feed.clock.now()) for i in range(10)])).decision
    assert d.budget_rps == 0 and d.reason_code is DecisionReason.NO_SAFE_BUDGET


def test_stale_untrusted_initializing_and_insufficient_evidence():
    feed, est = open_route()
    pool = [entry(i, feed.clock.now()) for i in range(10)]
    feed.clock.advance(20.0)
    stale = feed.decide()
    assert stale.mode is EstimatorMode.STALE
    d = decide(context(feed, stale, pool, decision_time=feed.clock.now())).decision
    assert (d.decision_mode, d.reason_code) == (
        DecisionMode.FAIL_CLOSED,
        DecisionReason.TELEMETRY_STALE,
    )
    poisoned = Feed()
    poisoned.push(120, **HEALTHY)
    bad = poisoned.push(60, payment_rps=35.0, throughput=70.0, p95_ms=300.0, timeouts=0, queue=5)
    assert bad.mode is EstimatorMode.FAIL_CLOSED
    pool_p = [entry(i, poisoned.clock.now()) for i in range(10)]
    d2 = decide(context(poisoned, bad, pool_p)).decision
    assert d2.reason_code is DecisionReason.TELEMETRY_UNTRUSTED and d2.budget_rps == 0
    fresh = Feed()
    init = fresh.push(3, **HEALTHY)
    d3 = decide(context(fresh, init, [entry(1, fresh.clock.now())])).decision
    assert d3.decision_mode is DecisionMode.WARMING_UP and d3.budget_rps == 0
    for decision in (d, d2, d3):
        assert decision.selected_transaction_ids == () and decision.state_versions == ()


def test_explicit_fail_closed_is_honoured_by_the_gate():
    feed, est = open_route()
    feed.estimator.latch_fail_closed()
    closed = feed.decide()
    ctx = context(feed, closed, [entry(i, feed.clock.now()) for i in range(10)])
    d = decide(ctx).decision
    assert d.decision_mode is DecisionMode.FAIL_CLOSED and d.budget_rps == 0
    plan = gate(ctx, d)
    assert plan.outcome is ControllerOutcome.EXPLICIT_FAIL_CLOSED and plan.admitted == ()
    assert plan.backend_fallback_permitted is False  # no unsafe fallback after FAIL_CLOSED


def test_no_eligible_transactions():
    feed, est = open_route()
    now = feed.clock.now()
    d = decide(context(feed, est, [entry(1, now, eligible_s=40.0)])).decision
    assert d.reason_code is DecisionReason.NO_ELIGIBLE_TRANSACTIONS and d.budget_rps == 0
    d2 = decide(context(feed, est, [])).decision
    assert d2.reason_code is DecisionReason.NO_ELIGIBLE_TRANSACTIONS


def test_objective_prefers_checks_when_they_are_useful_and_avoids_harm():
    feed, est = open_route()
    pool = [entry(i, feed.clock.now()) for i in range(40)]
    useful = decide(context(feed, est, pool, outcomes=GOOD_OUTCOMES))
    assert useful.decision.budget_rps > 0
    useless = decide(
        context(feed, est, pool, outcomes=CheckOutcomeCounts(authoritative=0, unknown=50))
    )
    assert useless.decision.budget_rps == 0  # checks that never answer only cost capacity
    zero = next(e for e in useful.evaluations if e.budget_rps == 0)
    best = next(e for e in useful.evaluations if e.budget_rps == useful.decision.budget_rps)
    assert best.objective < zero.objective - 0.5
    weights = PROFILES["balanced"]
    band = useful.forecast.budget(best.budget_rps).bands[0]
    terms = objective_terms(band, weights)
    assert terms["resolution_benefit"] == pytest.approx(-band.expected_authoritative)


def test_quota_and_credit_limits_bind_without_inventing_credit():
    feed, est = open_route()
    pool = [entry(i, feed.clock.now()) for i in range(40)]
    d = decide(context(feed, est, pool, credit_allowance=2)).decision
    assert len(d.selected_transaction_ids) <= 2
    d0 = decide(context(feed, est, pool, bank_quota={"A1": 0})).decision
    assert d0.budget_rps == 0 and d0.selected_transaction_ids == ()
    unlimited = decide(context(feed, est, pool)).decision
    assert len(unlimited.selected_transaction_ids) > 2  # None means no credit limit was provided


def test_wrong_route_or_experiment_is_rejected():
    feed, est = open_route()
    now = feed.clock.now()
    with pytest.raises(OptimizerInputError):
        decide(context(feed, est, [entry(1, now, route="A1-C")]))
    with pytest.raises(OptimizerInputError):
        decide(context(feed, est, [entry(1, now, exp="EXP-OTHER")]))
    with pytest.raises(OptimizerInputError):
        decide(context(feed, est, [], route_id="A1-C"))
    with pytest.raises(OptimizerInputError):
        decide(context(feed, est, [entry(1, now), entry(1, now)]))


def test_ablations_change_only_their_mechanism():
    feed, est = open_route()
    pool = [entry(i, feed.clock.now(), deadline_s=3000 - i) for i in range(60)]
    ctx = context(feed, est, pool)

    def run(ablation, **kw):
        return decide(ctx, OptimizerConfig(ablation=ablation, allow_research_ablations=True, **kw))

    full, nolook = run(Ablation.FULL), run(Ablation.NO_LOOKAHEAD)
    assert nolook.forecast is None and nolook.decision.budget_rps == nolook.cap_rps
    assert nolook.cap_rps == full.cap_rps  # same safety cap
    nofb = run(Ablation.NO_FEEDBACK)
    assert nofb.forecast.feedback_mode.value == "NO_FEEDBACK"
    nodl = run(Ablation.NO_DEADLINE_RANKING)
    assert nodl.decision.selected_transaction_ids[0] != full.decision.selected_transaction_ids[0]
    assert full.decision.selected_transaction_ids[0] == "T0059"  # earliest deadline first
    fixed = run(Ablation.FIXED_BUDGET, fixed_budget_rps=2)
    assert fixed.decision.budget_rps == 2 and fixed.research_only
    nub = run(Ablation.NO_UNCERTAINTY_BOUND)
    assert nub.research_only and nub.cap_rps >= full.cap_rps
    for r in (full, nolook, nofb, nodl, fixed, nub):
        assert gate(ctx, r.decision).refused == ()  # every ablation stays gate-valid
    with pytest.raises(OptimizerInputError):
        decide(ctx, OptimizerConfig(ablation=Ablation.FIXED_BUDGET))  # research only by default


def test_research_ablations_still_fail_closed():
    feed, _ = open_route()
    feed.estimator.latch_fail_closed()
    closed = feed.decide()
    ctx = context(feed, closed, [entry(i, feed.clock.now()) for i in range(10)])
    for ablation in Ablation:
        d = decide(ctx, OptimizerConfig(ablation=ablation, allow_research_ablations=True)).decision
        assert d.decision_mode is DecisionMode.FAIL_CLOSED and d.budget_rps == 0


def test_deterministic_decisions_and_ids():
    feed, est = open_route()
    ctx = context(feed, est, [entry(i, feed.clock.now()) for i in range(40)])
    a, b = decide(ctx).decision, decide(ctx).decision
    assert a == b and a.decision_id.startswith("dec-")
    other = decide(ctx, OptimizerConfig(profile=PROFILES["ambiguity_averse"])).decision
    assert other.decision_id != a.decision_id and "ambiguity_averse" in other.policy_version


def test_no_truth_backend_or_ledger_imports_and_no_twin_mutation():
    for rel in ("intelligence/controller/optimizer.py", "intelligence/controller/ranker.py"):
        tree = ast.parse((REPO / rel).read_text())
        modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        modules |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not [
            m
            for m in modules
            if (m or "").startswith(
                ("backend", "intelligence.simulation", "intelligence.data", "sqlalchemy", "psycopg")
            )
        ]
    twin, clock = _twin_with_pool()
    before = {t: twin.snapshot(EXP, t) for t in twin.transaction_ids(EXP)}
    pool_from_twin(twin, EXP, ROUTE, clock.now())
    assert {t: twin.snapshot(EXP, t) for t in twin.transaction_ids(EXP)} == before


def test_escalation_outranks_an_earlier_unescalated_deadline():
    now = Feed().clock.now()
    pool = [entry(1, now, deadline_s=200), entry(2, now, deadline_s=900, escalated=True)]
    assert rank_pool(pool, 0.5, LA)[0].transaction_id == "T0002"


def test_selection_never_repeats_a_transaction():
    now = Feed().clock.now()
    e = entry(1, now)
    assert select_for_dispatch((e, e, entry(2, now)), now, 5) == (e, entry(2, now))


def test_positive_budget_needs_a_material_improvement():
    feed, est = open_route()
    pool = [entry(i, feed.clock.now()) for i in range(40)]
    d = decide(context(feed, est, pool), OptimizerConfig(min_improvement=1e9)).decision
    assert d.budget_rps == 0 and d.reason_code is DecisionReason.NO_SAFE_BUDGET
