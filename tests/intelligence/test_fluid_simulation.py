"""Stage 3B: deterministic fluid-queue simulator. Groups A–H follow the Stage 3B test matrix.

Micro-workloads place individual payments at exact times (built from real Stage 3A records);
mechanism tests use Stage 3A generated workloads. Everything is deterministic.
"""

import json
import math
import random
import subprocess
import sys
import tracemalloc
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from contracts.v1_1 import CheckResultKind, RouteDescriptor, ValidatedRouteTelemetry
from intelligence.common import ManualClock
from intelligence.data.generate_dataset import (
    BeneficiaryTruth,
    CapacityChange,
    CustomerBehaviour,
    PaymentArrival,
    Workload,
    generate_workload,
)
from intelligence.simulation.engine import (
    CheckDecision,
    ControllerObservation,
    FluidSimulation,
    SimulationAccountingError,
    SimulationConfig,
    SimulationFinishedError,
    run,
    simulate,
)
from intelligence.simulation.scenarios import (
    BeneficiarySpec,
    CheckDemandSpec,
    FaultSpec,
    FlappingSpec,
    RepaySpec,
    RouteSpec,
    ScenarioConfig,
    ScenarioOptions,
    reference_scenario,
    research_timing_policy,
)
from intelligence.twin import IngestStatus, RouteTwinConfig, RouteTwinRegistry

REPO_ROOT = Path(__file__).resolve().parents[2]


def route(route_id="A1-B", payment_rps=0.0, capacity_rps=10.0, remitter="A1", beneficiary="B"):
    return RouteSpec(
        route_id=route_id,
        remitter_bank_id=remitter,
        beneficiary_bank_id=beneficiary,
        payment_rps=payment_rps,
        capacity_rps=capacity_rps,
    )


def cut(route_id, start, end, magnitude=0.35):
    return FaultSpec(
        fault_id=f"cut-{route_id}-{start}",
        route_id=route_id,
        kind="CAPACITY_CUT",
        start_s=start,
        end_s=end,
        magnitude=magnitude,
    )


def config(routes=None, faults=(), repay=0.0, duration=600.0, seed=5, **extra) -> ScenarioConfig:
    return ScenarioConfig(
        scenario_id="test",
        description="Stage 3B test scenario",
        reference="unit test",
        experiment_id="EXP-SIM",
        seed=seed,
        duration_s=duration,
        routes=routes or (route(),),
        faults=faults,
        repay=RepaySpec(probability=repay),
        beneficiary=BeneficiarySpec(base_rejection_prob=0.0),
        timing_policy=research_timing_policy(),
        **extra,
    )


def manual(payments, scenario: ScenarioConfig | None = None) -> Workload:
    """Exact payments on top of a real Stage 3A workload (its capacity schedule and faults)."""

    scenario = scenario or config()
    base = generate_workload(scenario)
    arrivals, truths, behaviours = [], [], []
    for n, spec in enumerate(payments):
        r = scenario.route(spec.get("route", "A1-B"))
        txn = spec.get("txn", f"M-{n}")
        offset = spec["offset"]
        arrivals.append(
            PaymentArrival(
                experiment_id=scenario.experiment_id,
                sequence=n,
                transaction_id=txn,
                idempotency_key=f"PAY-{txn}",
                route_id=r.route_id,
                remitter_bank_id=r.remitter_bank_id,
                beneficiary_bank_id=r.beneficiary_bank_id,
                amount_minor=50_000,
                offset_s=offset,
                arrival_at=scenario.epoch + timedelta(seconds=offset),
            )
        )
        truths.append(
            BeneficiaryTruth(
                experiment_id=scenario.experiment_id,
                transaction_id=txn,
                route_id=r.route_id,
                credited=spec.get("credited", True),
                beneficiary_latency_ms=spec.get("latency_ms", 100.0),
                response_lost=spec.get("lost", False),
                confirmation_delay_s=spec.get("confirm", 0.0),
                intrinsic_ambiguity=None,
                active_fault_ids=(),
            )
        )
        behaviours.append(
            CustomerBehaviour(
                experiment_id=scenario.experiment_id,
                transaction_id=txn,
                would_repay=spec.get("repay", False),
                repay_delay_s=spec.get("repay_delay", 30.0),
                would_resubmit_same_key=spec.get("resubmit", False),
                resubmit_delay_s=spec.get("resubmit_delay", 5.0),
            )
        )
    return base.model_copy(
        update={
            "exogenous": base.exogenous.model_copy(
                update={"payments": tuple(arrivals), "customer_behaviour": tuple(behaviours)}
            ),
            "ground_truth": base.ground_truth.model_copy(update={"beneficiary": tuple(truths)}),
        }
    )


def check_every_tick(obs: ControllerObservation | None):
    """External schedule: ask for one check per outstanding ambiguous transaction."""

    if obs is None:
        return ()
    return [
        CheckDecision(route_id=r, count=len(ids)) for r, ids in obs.ambiguous_outstanding if ids
    ]


def flood(obs: ControllerObservation | None):
    if obs is None:
        return ()
    return [CheckDecision(route_id=r, count=10**6) for r, ids in obs.ambiguous_outstanding if ids]


def run_observed(workload, policy=None, **cfg):
    sim = FluidSimulation(workload, SimulationConfig(**cfg))
    observations = []
    while not sim.done:
        decisions = policy(sim.last_observation) if policy else ()
        observations.append(sim.step(decisions))
        sim.check_invariants()
    return sim.result(), observations


def route_metrics(result, route_id="A1-B"):
    return [m for m in result.metrics if m.route_id == route_id]


def recovery_time(result, route_id, fault_end, hold=60, threshold=2.0):
    metrics = [m for m in route_metrics(result, route_id) if m.start_s >= fault_end]
    for i, m in enumerate(metrics):
        window = metrics[i : i + hold]
        if len(window) == hold and all((x.est_queue_delay_s or 0.0) < threshold for x in window):
            return m.start_s - fault_end
    return None


FAULTED = config(
    routes=(route(payment_rps=7.0),), faults=(cut("A1-B", 100.0, 400.0),), duration=900
)


@pytest.fixture(scope="module")
def faulted_workload():
    return generate_workload(FAULTED)


# ============================================================ A — model foundations


def test_a01_empty_workload():
    result, observations = run_observed(manual([]))
    assert result.ticks == 600 and len(result.metrics) == 600
    assert all(v == 0 for k, v in result.totals if k != "ticks")
    assert all(o.telemetry[0].latency_sample_count == 0 for o in observations)


def test_a02_zero_arrivals_with_check_requests_are_denied():
    sim = FluidSimulation(manual([]))
    sim.step([CheckDecision(route_id="A1-B", count=5)])
    result = sim.result()
    assert result.total("checks_released") == 0 and result.total("checks_denied") == 5


def test_a03_normal_capacity_serves_every_payment_in_time():
    workload = generate_workload(config(routes=(route(payment_rps=7.0),)))
    result = simulate(workload)
    settled = sum(1 for p in workload.exogenous.payments if p.offset_s < 598.0)
    in_flight = result.total("payment_arrivals") - settled  # last two seconds still in flight
    assert result.total("timely_successes") >= settled > 3500
    assert result.total("timely_successes") + in_flight >= result.total("payment_arrivals")
    assert result.total("new_ambiguous") == 0 and result.total("unresolved_at_end") == 0
    assert max(m.queue_depth for m in result.metrics) < 25


def test_a04_zero_capacity_serves_nothing_and_everything_times_out():
    scenario = config(
        routes=(route(payment_rps=2.0),), faults=(cut("A1-B", 0.0, 600.0, 1.0),), duration=300
    )
    result = simulate(generate_workload(scenario), SimulationConfig(stale_drop_age_s=None))
    assert result.total("served") == 0
    arrivals = result.total("payment_arrivals")
    assert result.total("queued_at_end") == arrivals
    deadline_passed = sum(
        1 for p in generate_workload(scenario).exogenous.payments if p.offset_s + 15.0 < 300
    )
    assert result.total("new_ambiguous") == deadline_passed


@pytest.mark.parametrize("dt", [0.25, 0.5, 1.0])
def test_a05_fractional_timesteps_conserve_work(faulted_workload, dt):
    result, _ = run_observed(faulted_workload, dt_s=dt)
    assert result.ticks == round(900 / dt)
    assert result.total("payment_arrivals") == len(faulted_workload.exogenous.payments)
    queued = result.total("queued_at_end")
    assert result.total("served") + result.total("dropped") + queued == sum(
        result.total(f"arrived_{c}")
        for c in ("payment", "repayment", "resubmission", "status_check", "background_check")
    )


def test_a06_queue_grows_under_overload():
    scenario = config(routes=(route(payment_rps=7.0, capacity_rps=5.0),), duration=300)
    result = simulate(generate_workload(scenario), SimulationConfig(stale_drop_age_s=None))
    depth = [m.queue_depth for m in result.metrics]
    assert depth[299] > depth[150] > depth[50]
    assert 450 < depth[299] < 750  # about (7 - 5) requests/s for 300 s


def test_a07_queue_drains_when_headroom_returns(faulted_workload):
    result = simulate(faulted_workload)
    metrics = route_metrics(result)
    assert max(m.queue_depth for m in metrics if 100 <= m.start_s < 400) > 100
    assert max(m.queue_depth for m in metrics if m.start_s >= 600) < 25


def test_a08_queue_depth_and_delay_are_never_negative(faulted_workload):
    result = simulate(faulted_workload, policy=check_every_tick)
    assert all(m.queue_depth >= 0 and m.bottleneck_queue_depth >= 0 for m in result.metrics)
    assert all(m.est_queue_delay_s is None or m.est_queue_delay_s >= 0 for m in result.metrics)


def test_a09_every_request_is_conserved(faulted_workload):
    result, _ = run_observed(faulted_workload, policy=flood, enforce_timing_policy=False)
    classes = ("payment", "repayment", "resubmission", "status_check", "background_check")
    waiting = {
        c: result.total(f"arrived_{c}")
        - result.total(f"completed_{c}")
        - result.total(f"dropped_{c}")
        for c in classes
    }
    assert all(n >= 0 for n in waiting.values())
    assert sum(waiting.values()) == result.total("queued_at_end")
    assert result.total("dropped") == sum(result.total(f"dropped_{c}") for c in classes) > 0
    assert result.total("served") == sum(
        result.total(f"completed_{c}")
        for c in ("payment", "repayment", "resubmission", "status_check", "background_check")
    )


def test_a10_units_latency_and_service_conversion():
    result, observations = run_observed(manual([{"offset": 0.0, "latency_ms": 250.0}]))
    # Served at the end of tick 0 (t = 1 s), response 0.25 s later: 1250 ms after sending.
    telemetry = observations[1].telemetry[0]
    assert telemetry.p50_latency_ms == pytest.approx(1250.0)
    assert telemetry.completed_payment_rps == 1.0
    scenario = config(routes=(route(payment_rps=40.0, capacity_rps=10.0),), duration=20)
    half = simulate(generate_workload(scenario), SimulationConfig(dt_s=0.5))
    assert {m.served for m in half.metrics[2:]} == {5}  # 10 requests/s x 0.5 s


# ============================================================ B — capacity faults


def test_b01_b05_fault_starts_and_ends_on_schedule(faulted_workload):
    metrics = route_metrics(simulate(faulted_workload))
    by_start = {m.start_s: m for m in metrics}
    assert by_start[99.0].capacity_rps == 10.0 and not by_start[99.0].fault_active
    assert by_start[100.0].capacity_rps == pytest.approx(6.5) and by_start[100.0].fault_active
    assert by_start[399.0].capacity_rps == pytest.approx(6.5) and by_start[399.0].fault_active
    assert by_start[400.0].capacity_rps == 10.0 and not by_start[400.0].fault_active


def test_b03_capacity_is_averaged_over_half_open_intervals():
    scenario = config(faults=(cut("A1-B", 100.5, 200.25),), duration=300)
    metrics = {m.start_s: m for m in simulate(generate_workload(scenario)).metrics}
    assert metrics[100.0].capacity_rps == pytest.approx(10 * 0.5 + 6.5 * 0.5)
    assert metrics[101.0].capacity_rps == pytest.approx(6.5)
    assert metrics[200.0].capacity_rps == pytest.approx(6.5 * 0.25 + 10 * 0.75)
    assert metrics[201.0].capacity_rps == 10.0


def test_b06_flapping_capacity():
    flap = FlappingSpec(
        flapping_id="flap", route_id="A1-B", start_s=100.0, end_s=500.0, period_s=100.0,
        dip_duration_s=20.0, dip_fraction=0.3,
    )  # fmt: skip
    metrics = {m.start_s: m for m in simulate(generate_workload(config(flapping=(flap,)))).metrics}
    assert metrics[105.0].capacity_rps == pytest.approx(7.0)
    assert metrics[125.0].capacity_rps == 10.0
    assert metrics[205.0].capacity_rps == pytest.approx(7.0)


def test_b07_healthy_parallel_route_is_isolated():
    scenario = config(
        routes=(route(payment_rps=7.0), route("A1-C", 7.0, beneficiary="C")),
        faults=(cut("A1-B", 100.0, 400.0),),
        duration=900,
    )
    result = simulate(generate_workload(scenario))
    assert sum(m.new_ambiguous for m in route_metrics(result, "A1-B")) > 500
    assert sum(m.new_ambiguous for m in route_metrics(result, "A1-C")) == 0
    assert {m.capacity_rps for m in route_metrics(result, "A1-C")} == {10.0}


def test_b08_shared_bottleneck_spreads_contention():
    scenario = config(
        routes=(route(payment_rps=4.0, capacity_rps=5.0), route("A2-C", 4.0, 5.0, "A2", "C")),
        faults=(cut("A1-B", 100.0, 400.0, 0.5),),
        duration=900,
    )
    workload = generate_workload(scenario)
    independent = simulate(workload)
    shared = simulate(workload, SimulationConfig(bottleneck_groups=(("A1-B", "A2-C"),)))
    assert sum(m.new_ambiguous for m in route_metrics(independent, "A2-C")) == 0
    assert sum(m.new_ambiguous for m in route_metrics(shared, "A2-C")) > 50
    a = route_metrics(shared, "A1-B")
    b = route_metrics(shared, "A2-C")
    assert all(x.est_queue_delay_s == y.est_queue_delay_s for x, y in zip(a, b, strict=True))


# ============================================================ C — payment outcomes


def test_c01_timely_success_is_observed():
    result, observations = run_observed(manual([{"offset": 0.2}]))
    (outcome,) = [o for obs in observations for o in obs.outcomes]
    assert (outcome.outcome, outcome.how) == ("SUCCESS", "TIMELY_RESPONSE")
    assert result.total("timely_successes") == 1 and result.total("new_ambiguous") == 0


def test_c02_timeout_leaves_the_outcome_uncertain():
    scenario = config(faults=(cut("A1-B", 0.0, 600.0, 1.0),))
    result, observations = run_observed(manual([{"offset": 0.2}], scenario))
    assert [t for obs in observations for t in obs.newly_ambiguous] == ["M-0"]
    assert observations[15].newly_ambiguous == ("M-0",)  # deadline 15.2 s, in tick 15
    assert not any(obs.outcomes for obs in observations)


def test_c03_lost_confirmation_makes_a_credited_payment_ambiguous():
    result, observations = run_observed(manual([{"offset": 0.2, "lost": True}]))
    assert result.total("new_ambiguous") == 1
    assert result.total("timely_successes") == 0
    assert result.total("unresolved_at_end") == 1  # nobody checked; credit never observed


def test_c04_delayed_beneficiary_response_resolves_late():
    result, observations = run_observed(manual([{"offset": 0.2, "latency_ms": 20_000.0}]))
    (late,) = [o for obs in observations for o in obs.outcomes]
    assert late.how == "LATE_RESPONSE" and late.observed_at.second == 21
    assert result.total("new_ambiguous") == 1 and result.total("late_responses") == 1


def test_c05_beneficiary_failure_is_a_timely_failure_not_ambiguity():
    result, observations = run_observed(manual([{"offset": 0.2, "credited": False}]))
    (outcome,) = [o for obs in observations for o in obs.outcomes]
    assert outcome.outcome == "FAILURE" and result.total("timely_failures") == 1
    assert result.total("new_ambiguous") == 0


def test_c06_no_reversal_or_money_operation_exists():
    public = {name for name in dir(FluidSimulation) if not name.startswith("_")}
    assert public == {
        "check_invariants",
        "done",
        "last_observation",
        "metrics_until",
        "now",
        "result",
        "run",
        "service_accounting",
        "step",
        "tick",
    }
    scenario = config(faults=(cut("A1-B", 0.0, 600.0, 1.0),))
    result, observations = run_observed(manual([{"offset": 0.2}], scenario))
    assert not any(o.outcome == "FAILURE" for obs in observations for o in obs.outcomes)


def test_c07_a_transaction_becomes_ambiguous_only_once():
    payments = [{"offset": 0.2, "latency_ms": 20_000.0}, {"offset": 0.4, "lost": True}]
    result, observations = run_observed(manual(payments), check_every_tick)
    ambiguous = [t for obs in observations for t in obs.newly_ambiguous]
    assert sorted(ambiguous) == ["M-0", "M-1"]
    assert result.total("new_ambiguous") == 2


def test_c08_repayments_arise_only_from_simulated_ambiguity():
    normal = config(routes=(route(payment_rps=7.0),), repay=1.0)
    assert simulate(generate_workload(normal)).total("repayment_arrivals") == 0
    blocked = config(
        routes=(route(payment_rps=2.0),), faults=(cut("A1-B", 0.0, 600.0, 1.0),), repay=1.0
    )
    result, observations = run_observed(generate_workload(blocked))
    repays = [t for obs in observations for t in obs.sent_transaction_ids if t.startswith("RP-")]
    ambiguous = {t for obs in observations for t in obs.newly_ambiguous}
    assert repays and all(t.removeprefix("RP-") in ambiguous for t in repays)


def test_c09_repayments_follow_pre_drawn_intent_under_any_policy():
    scenario = FAULTED.model_copy(update={"repay": RepaySpec(probability=0.3)})
    workload = generate_workload(scenario)
    intent = {b.transaction_id: b.would_repay for b in workload.exogenous.customer_behaviour}
    for policy in (None, check_every_tick):
        _, observations = run_observed(workload, policy)
        repaid = [t.removeprefix("RP-") for o in observations for t in o.sent_transaction_ids
                  if t.startswith("RP-")]  # fmt: skip
        assert repaid and all(intent[t] for t in repaid)
        assert len(repaid) == len(set(repaid))


def test_c10_repayments_consume_capacity(faulted_workload):
    with_repay = generate_workload(FAULTED.model_copy(update={"repay": RepaySpec(probability=1.0)}))
    plain = simulate(faulted_workload)
    amplified = simulate(with_repay)
    assert amplified.total("repayment_arrivals") > 500
    assert amplified.total("completed_repayment") > 0
    assert max(m.queue_depth for m in amplified.metrics) >= max(
        m.queue_depth for m in plain.metrics
    )


# ============================================================ D — status checks


def test_d01_no_check_policy_sends_no_checks(faulted_workload):
    result = simulate(faulted_workload)
    assert result.total("checks_released") == 0 and result.total("wasted_checks") == 0


def test_d02_injected_schedule_releases_exactly_the_eligible_checks():
    payments = [{"offset": 0.2, "lost": True}, {"offset": 0.5, "lost": True}]
    sim = FluidSimulation(manual(payments))
    for _ in range(91):
        sim.step()
    observation = sim.step([CheckDecision(route_id="A1-B", transaction_ids=("M-1", "M-0"))])
    result = sim.result()
    assert result.total("checks_released") == 2
    assert observation.denied_checks == ()


def test_d02b_ineligible_checks_are_denied_by_the_timing_policy():
    sim = FluidSimulation(manual([{"offset": 0.2, "lost": True}]))
    for _ in range(20):
        sim.step()
    observation = sim.step([CheckDecision(route_id="A1-B", transaction_ids=("M-0",))])
    assert observation.denied_checks == ("M-0",)  # first check only from 90 s after sending
    assert sim.result().total("checks_released") == 0


def test_d03_checks_consume_capacity():
    scenario = config(routes=(route(capacity_rps=1.0),), duration=200)
    payments = [{"offset": 0.1 * n, "lost": True} for n in range(5)]
    payments += [{"offset": 92.0, "txn": "LATE"}]
    workload = manual(payments, scenario)

    def late_answer(policy):
        _, observations = run_observed(workload, policy)
        (late,) = [o for obs in observations for o in obs.outcomes if o.transaction_id == "LATE"]
        return late.observed_at.second + late.observed_at.microsecond / 1e6

    # Alone, LATE is served at the end of its tick (93 s) and answered 0.1 s later. The five
    # checks released at 90-91 s queue ahead of it on the 1 request/s capacity.
    assert late_answer(None) == pytest.approx(33.1)  # 93.1 s = 00:01:33.1
    assert late_answer(check_every_tick) == pytest.approx(36.1)


def test_d04_d09_completed_check_returns_the_authoritative_outcome():
    result, observations = run_observed(manual([{"offset": 0.2, "lost": True}]), check_every_tick)
    (answer,) = [c for obs in observations for c in obs.check_results]
    assert answer.result is CheckResultKind.AUTHORITATIVE_SUCCESS
    assert result.total("resolved_by_check") == 1 and result.total("unresolved_at_end") == 0


def test_d05_d06_check_times_out_and_is_later_dropped_without_double_counting():
    scenario = config(faults=(cut("A1-B", 50.0, 600.0, 1.0),))
    result, observations = run_observed(manual([{"offset": 0.2, "lost": True}], scenario),
                                        check_every_tick)  # fmt: skip
    timeouts = [c for obs in observations for c in obs.check_timeouts]
    assert timeouts and result.total("wasted_checks") == len(timeouts)
    assert result.total("dropped_status_check") >= 1
    assert result.total("check_results") == 0


def test_d07_d10_pending_result_never_reveals_the_hidden_credit():
    result, observations = run_observed(manual([{"offset": 0.2, "latency_ms": 200_000.0}]),
                                        check_every_tick)  # fmt: skip
    first = next(c for obs in observations for c in obs.check_results)
    assert first.result is CheckResultKind.PENDING  # truth is credited, but not yet decided
    resolution = next(o for obs in observations for o in obs.outcomes)
    assert resolution.observed_at > first.observed_at
    assert resolution.how in ("LATE_RESPONSE", "CHECK")


def test_d08_check_on_a_never_forwarded_payment_is_unknown():
    scenario = config(faults=(cut("A1-B", 0.0, 60.0, 1.0),))
    _, observations = run_observed(manual([{"offset": 0.2}], scenario), check_every_tick)
    (answer,) = [c for obs in observations for c in obs.check_results][:1]
    assert answer.result is CheckResultKind.UNKNOWN


# ============================================================ E — feedback


def test_e01_e02_checks_add_load_and_backlog_under_overload(faulted_workload):
    calm = simulate(faulted_workload)
    stormy = simulate(faulted_workload, SimulationConfig(enforce_timing_policy=False), flood)
    assert stormy.total("completed_status_check") > 0
    assert max(m.queue_depth for m in stormy.metrics) > 5 * max(m.queue_depth for m in calm.metrics)


def test_e03_overload_increases_payment_timeouts(faulted_workload):
    healthy = generate_workload(FAULTED.model_copy(update={"faults": ()}))
    assert simulate(healthy).total("new_ambiguous") == 0
    assert simulate(faulted_workload).total("new_ambiguous") > 500


def test_e04_ambiguity_creates_future_recovery_demand():
    lossy = config(
        routes=(route(payment_rps=7.0),),
        faults=(
            FaultSpec(
                fault_id="loss",
                route_id="A1-B",
                kind="RESPONSE_LOSS",
                start_s=100.0,
                end_s=200.0,
                magnitude=1.0,
            ),
        ),  # fmt: skip
    )
    result = simulate(generate_workload(lossy), policy=check_every_tick)
    released = [(m.start_s, m.checks_released) for m in result.metrics if m.checks_released]
    assert released and min(t for t, _ in released) >= 190.0  # 90 s after the first loss
    healthy = simulate(
        generate_workload(lossy.model_copy(update={"faults": ()})), policy=check_every_tick
    )
    assert healthy.total("checks_released") == 0


def test_e05_e06_sustained_checking_delays_recovery(faulted_workload):
    quiet = simulate(faulted_workload)
    storm = simulate(faulted_workload, SimulationConfig(enforce_timing_policy=False), flood)
    quiet_recovery = recovery_time(quiet, "A1-B", 400.0)
    assert quiet_recovery is not None and quiet_recovery < 200
    assert recovery_time(storm, "A1-B", 400.0) is None  # never recovers within the run
    assert storm.total("wasted_checks") > 10 * storm.total("check_results")


def test_e07_uncontrolled_repayments_amplify_overload(faulted_workload):
    repaying = generate_workload(FAULTED.model_copy(update={"repay": RepaySpec(probability=1.0)}))
    plain, amplified = simulate(faulted_workload), simulate(repaying)
    assert amplified.total("new_ambiguous") > plain.total("new_ambiguous")
    assert recovery_time(amplified, "A1-B", 400.0) > recovery_time(plain, "A1-B", 400.0)


def test_e08_zero_check_control_recovers_through_late_responses(faulted_workload):
    result = simulate(faulted_workload)
    assert result.total("checks_released") == 0
    assert result.total("resolved_by_late_response") == result.total("resolved") > 0
    assert route_metrics(result)[-1].queue_depth < 25


# ============================================================ F — reproducibility


def test_f01_same_inputs_give_identical_results(faulted_workload):
    first = simulate(faulted_workload, policy=check_every_tick)
    second = simulate(faulted_workload, policy=check_every_tick)
    assert first == second and first.digest == second.digest


def test_f02_different_workload_seeds_differ():
    a = simulate(generate_workload(FAULTED))
    b = simulate(generate_workload(FAULTED.model_copy(update={"seed": 6})))
    assert a.digest != b.digest


def test_f03_exogenous_arrivals_are_identical_across_policies(faulted_workload):
    quiet = simulate(faulted_workload)
    storm = simulate(faulted_workload, SimulationConfig(enforce_timing_policy=False), flood)
    assert [m.payment_arrivals for m in quiet.metrics] == [
        m.payment_arrivals for m in storm.metrics
    ]


def test_f04_no_wall_clock_dependency():
    past = datetime(2001, 5, 6, tzinfo=UTC)
    workload = generate_workload(config(routes=(route(payment_rps=2.0),), epoch=past, duration=60))
    sim = FluidSimulation(workload)
    observation = sim.step()
    assert observation.window_start == past
    source = (REPO_ROOT / "intelligence/simulation/engine.py").read_text()
    for forbidden in ("datetime.now", "utcnow", "time.time", "uuid", "import random"):
        assert forbidden not in source


def test_f05_global_random_state_is_untouched(faulted_workload):
    random.seed(1234)
    state = random.getstate()
    simulate(faulted_workload, policy=check_every_tick)
    assert random.getstate() == state


TRUTH_KEYS = {
    "credited",
    "capacity_rps",
    "response_lost",
    "confirmation_delay_s",
    "would_repay",
    "would_resubmit",
    "fault_active",
    "est_queue_delay_s",
    "bottleneck_queue_depth",
    "processed_s",
}


def _keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list | tuple):
        return {k for v in value for k in _keys(v)}
    return set()


def test_f06_policies_only_see_controller_observations(faulted_workload):
    seen = []

    def spy(obs):
        seen.append(obs)
        assert obs is None or isinstance(obs, ControllerObservation)
        return ()

    sim = FluidSimulation(faulted_workload)
    sim.run(spy)
    observation = seen[-1]
    data = json.loads(FluidSimulation._observation_json(observation))
    assert _keys(data).isdisjoint(TRUTH_KEYS)
    assert all(t.observation_scope == "REMITTER_OWN_TRAFFIC" for t in observation.telemetry)


def test_f07_simulations_share_no_state(faulted_workload):
    first = FluidSimulation(faulted_workload)
    second = FluidSimulation(faulted_workload.model_copy())
    for _ in range(200):
        first.step()
    assert second.tick == 0 and second.last_observation is None
    assert simulate(faulted_workload) == second.run()


# ============================================================ G — integration


@pytest.fixture(scope="module")
def s2_small():
    return generate_workload(
        reference_scenario(
            "S2",
            seed=11,
            experiment_id="EXP-3B",
            rate_scale=0.01,
            duration_s=2400.0,
            options=ScenarioOptions(healthy_parallel_route=True),
        )
    )


def test_g01_g02_consumes_stage_3a_records_and_keeps_identity(s2_small):
    result = simulate(s2_small)
    manifest = s2_small.manifest
    assert (result.run_id, result.workload_content_hash, result.experiment_id) == (
        manifest.run_id,
        manifest.content_hash,
        manifest.experiment_id,
    )
    assert result.total("payment_arrivals") == len(s2_small.exogenous.payments)


def test_g03_g04_observations_feed_the_route_twin(s2_small):
    descriptors: list[RouteDescriptor] = list(s2_small.manifest.config.descriptors())
    clock = ManualClock(s2_small.manifest.config.epoch)
    twin = RouteTwinRegistry(
        RouteTwinConfig(
            model_version="sim-1",
            max_observation_age_s=5.0,
            max_future_skew_s=1.0,
            freshness_threshold_s=5.0,
        ),
        clock,
    )
    for descriptor in descriptors:
        twin.register_route(descriptor, [f"sim-remitter-{descriptor.route_id}"])
    sim = FluidSimulation(s2_small)
    for _ in range(120):
        observation = sim.step()
        clock.set_time(observation.window_end)
        for telemetry in observation.telemetry:
            envelope = ValidatedRouteTelemetry(
                schema_version="1.1",
                experiment_id=telemetry.experiment_id,
                route_id=telemetry.route_id,
                telemetry=telemetry,
                trust_status="AUTHENTICATED",
                validator_id="sim",
                validator_version="1",
                validated_at=telemetry.observation_timestamp,
                cross_check_status="CONSISTENT",
            )
            assert twin.ingest(envelope).status is IngestStatus.APPLIED
    assert {d.route_id for d in descriptors} == {"A1-B", "A1-C"}
    assert twin.snapshot("EXP-3B", "A1-B").freshness_status == "FRESH"


def test_g05_runs_without_track_b():
    code = (
        "import sys, intelligence.simulation.engine\n"
        "bad = sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'backend', 'sqlalchemy', 'psycopg', 'boto3', 'httpx', 'requests', 'simpy'} "
        "or m.startswith(('intelligence.controller', 'intelligence.ml', 'intelligence.twin')))\n"
        "print(','.join(bad))\n"
    )
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and constant code
        [sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    assert completed.stdout.strip() == ""


def test_g06_legacy_demonstration_still_runs():
    ticks = run()
    assert len(ticks) == 30 and ticks == run()


# ============================================================ H — robustness


def _with_capacity(workload: Workload, value) -> Workload:
    bad = CapacityChange.model_construct(
        **{**workload.ground_truth.capacity[0].__dict__, "capacity_rps": value}
    )
    return workload.model_copy(
        update={"ground_truth": workload.ground_truth.model_copy(update={"capacity": (bad,)})}
    )


@pytest.mark.parametrize("value", [-1.0, math.nan, math.inf])
def test_h01_h02_invalid_capacity_is_rejected(value):
    with pytest.raises(ValueError, match="invalid capacity"):
        FluidSimulation(_with_capacity(manual([]), value))


@pytest.mark.parametrize("dt", [0.0, -1.0, math.nan, math.inf, 16.0])
def test_h03_invalid_timestep_is_rejected(dt):
    with pytest.raises((ValidationError, ValueError)):
        FluidSimulation(manual([]), SimulationConfig(dt_s=dt))


def test_h04_duplicate_payment_identifiers_are_rejected():
    workload = manual([{"offset": 0.2, "txn": "DUP"}, {"offset": 0.4, "txn": "X"}])
    duplicated = workload.model_copy(
        update={
            "exogenous": workload.exogenous.model_copy(
                update={"payments": workload.exogenous.payments[:1] * 2}
            )
        }
    )
    with pytest.raises(ValueError, match="duplicate"):
        FluidSimulation(duplicated)


def test_h05_decisions_for_unknown_routes_are_rejected():
    sim = FluidSimulation(manual([]))
    with pytest.raises(ValueError, match="unknown route"):
        sim.step([CheckDecision(route_id="Z9-Q", count=1)])
    with pytest.raises(ValidationError):
        CheckDecision(route_id="A1-B", count=1, transaction_ids=("M-0",))


def test_h06_huge_check_requests_do_not_grow_memory(faulted_workload):
    sim = FluidSimulation(faulted_workload, SimulationConfig(enforce_timing_policy=False))
    tracemalloc.start()
    for _ in range(300):
        sim.step([CheckDecision(route_id="A1-B", count=10**9)])
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 50_000_000
    assert sim.result().total("checks_denied") > 10**9


def test_h07_inconsistent_accounting_is_detected(faulted_workload):
    sim = FluidSimulation(faulted_workload)
    for _ in range(150):
        sim.step()
    sim.check_invariants()
    sim._queued["PAYMENT"] += 1  # simulate a corrupted counter
    with pytest.raises(SimulationAccountingError):
        sim.check_invariants()


def test_h08_no_state_beyond_the_horizon():
    sim = FluidSimulation(manual([]), SimulationConfig(horizon_s=10.0))
    sim.run()
    with pytest.raises(SimulationFinishedError):
        sim.step()
    with pytest.raises(SimulationFinishedError):
        FluidSimulation(manual([])).metrics_until(5)


def test_h09_empty_outputs_remain_valid():
    observation = FluidSimulation(manual([])).step()
    telemetry = observation.telemetry[0]
    assert telemetry.timeout_rate is None and telemetry.p95_latency_ms is None
    assert observation.newly_ambiguous == () and observation.outcomes == ()


def test_h10_coincident_boundaries_are_deterministic():
    scenario = config(
        faults=(cut("A1-B", 15.0, 30.0, 1.0),),
        check_demand=(
            CheckDemandSpec(
                demand_id="bg",
                bank_id="A3",
                route_id="A1-B",
                start_s=15.0,
                end_s=30.0,
                rate_rps=3.0,
            ),
        ),  # fmt: skip
        duration=200,
    )
    payments = [{"offset": 0.0, "latency_ms": 15_000.0}, {"offset": 15.0}]
    workload = manual(payments, scenario)
    first, obs_first = run_observed(workload)
    second, _ = run_observed(workload)
    assert first == second
    # Served at 1 s with 15 s latency the response lands at 16 s, after the 15 s deadline.
    assert [t for o in obs_first for t in o.newly_ambiguous][:1] == ["M-0"]


# ============================================================ boundary and policy details


def test_a11_fractional_service_progress_carries_across_ticks():
    scenario = config(routes=(route(capacity_rps=0.4),), duration=30)
    _, observations = run_observed(manual([{"offset": 0.0}], scenario))
    (outcome,) = [o for obs in observations for o in obs.outcomes]
    # 0.4 + 0.4 + 0.4 units: the request completes at the end of tick 2, answered 0.1 s later.
    assert outcome.observed_at.second + outcome.observed_at.microsecond / 1e6 == pytest.approx(3.1)


def test_a12_stale_cohorts_are_dropped_exactly_after_the_age_limit():
    scenario = config(faults=(cut("A1-B", 0.0, 600.0, 1.0),), duration=60)
    result, _ = run_observed(manual([{"offset": 0.0}], scenario), dt_s=0.5)
    (drop,) = [m.start_s for m in result.metrics if m.dropped]
    assert drop == 30.5  # cohort of t=0 is older than 30 s from the interval starting at 30.5 s


def test_d11_timing_policy_spacing_and_attempt_cap():
    scenario = config(duration=1000)
    workload = manual([{"offset": 0.0, "latency_ms": 3_000_000.0}], scenario)
    result, observations = run_observed(workload, check_every_tick)
    released = [m.start_s for m in result.metrics if m.checks_released]
    assert released == [90.0, 180.0, 270.0]  # 90 s first delay and spacing, at most 3 attempts
    answers = [c.result for obs in observations for c in obs.check_results]
    assert answers == [CheckResultKind.PENDING] * 3
    assert sum(len(obs.denied_checks) for obs in observations) > 600


def test_d12_idempotent_resubmission_does_not_reprocess_the_payment():
    payments = [{"offset": 0.0, "latency_ms": 20_000.0, "lost": True, "resubmit": True}]
    sim = FluidSimulation(manual(payments), SimulationConfig(enforce_timing_policy=False))
    for _ in range(25):
        sim.step()
    observation = sim.step([CheckDecision(route_id="A1-B", transaction_ids=("M-0",))])
    while not observation.check_results:
        observation = sim.step()
    # Decided at 21 s by the original forwarding; the re-submission must not move that to 41 s.
    assert observation.check_results[0].result is CheckResultKind.AUTHORITATIVE_SUCCESS
    assert sim.result().total("resubmission_arrivals") == 1
