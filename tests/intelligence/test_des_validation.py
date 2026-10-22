"""Stage 04: independent per-request discrete-event model (SimPy).

Micro-workloads place payments at exact, binary-exact times so boundaries can be tested without
floating-point ambiguity. Everything is deterministic.
"""

import json
import random
import subprocess
import sys
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

import pytest

pytest.importorskip("simpy")  # declared in pyproject's ``research`` extra (simpy==4.1.1)

from contracts.v1_1 import CheckResultKind  # noqa: E402
from intelligence.data.generate_dataset import (  # noqa: E402
    BeneficiaryTruth,
    CapacityChange,
    CheckDemandArrival,
    CustomerBehaviour,
    PaymentArrival,
    Workload,
    generate_workload,
)
from intelligence.simulation.des_validate import (  # noqa: E402
    CheckRelease,
    DesConfig,
    DesError,
    DesObservation,
    DesResult,
    DiscreteEventSimulation,
    ServiceDistribution,
    _Bottleneck,
    conservation_residuals,
    simulate_des,
)
from intelligence.simulation.engine import simulate  # noqa: E402
from intelligence.simulation.scenarios import (  # noqa: E402
    BeneficiarySpec,
    FaultSpec,
    RepaySpec,
    RouteSpec,
    ScenarioConfig,
    research_timing_policy,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _rng() -> random.Random:
    return random.Random(0)  # noqa: S311 - unused service stream in a pure-math test


def route(route_id="A1-B", payment_rps=0.0, capacity_rps=4.0, remitter="A1", beneficiary="B"):
    return RouteSpec(
        route_id=route_id,
        remitter_bank_id=remitter,
        beneficiary_bank_id=beneficiary,
        payment_rps=payment_rps,
        capacity_rps=capacity_rps,
    )


def cut(start, end, magnitude=0.5, route_id="A1-B", kind="CAPACITY_CUT"):
    return FaultSpec(fault_id=f"f-{start}-{kind.lower()}", route_id=route_id, kind=kind,
                     start_s=start, end_s=end, magnitude=magnitude)  # fmt: skip


def config(routes=None, faults=(), repay=0.0, duration=200.0, seed=5, **extra) -> ScenarioConfig:
    return ScenarioConfig(
        scenario_id="des-test",
        description="Stage 04 test scenario",
        reference="unit test",
        experiment_id="EXP-DES",
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
        txn, offset = spec.get("txn", f"M-{n}"), spec["offset"]
        arrivals.append(
            PaymentArrival(
                experiment_id=scenario.experiment_id, sequence=n, transaction_id=txn,
                idempotency_key=f"PAY-{txn}", route_id=r.route_id,
                remitter_bank_id=r.remitter_bank_id, beneficiary_bank_id=r.beneficiary_bank_id,
                amount_minor=50_000, offset_s=offset,
                arrival_at=scenario.epoch + timedelta(seconds=offset),
            )
        )  # fmt: skip
        truths.append(
            BeneficiaryTruth(
                experiment_id=scenario.experiment_id, transaction_id=txn, route_id=r.route_id,
                credited=spec.get("credited", True),
                beneficiary_latency_ms=spec.get("latency_ms", 250.0),
                response_lost=spec.get("lost", False),
                confirmation_delay_s=spec.get("confirm", 0.0),
                intrinsic_ambiguity=None, active_fault_ids=(),
            )
        )  # fmt: skip
        behaviours.append(
            CustomerBehaviour(
                experiment_id=scenario.experiment_id, transaction_id=txn,
                would_repay=spec.get("repay", False),
                repay_delay_s=spec.get("repay_delay", 30.0),
                would_resubmit_same_key=spec.get("resubmit", False),
                resubmit_delay_s=spec.get("resubmit_delay", 5.0),
            )
        )  # fmt: skip
    return base.model_copy(
        update={
            "exogenous": base.exogenous.model_copy(
                update={"payments": tuple(arrivals), "customer_behaviour": tuple(behaviours)}
            ),
            "ground_truth": base.ground_truth.model_copy(update={"beneficiary": tuple(truths)}),
        }
    )


def run(workload, schedule=(), **cfg) -> tuple[DesResult, DiscreteEventSimulation]:
    sim = DiscreteEventSimulation(workload, DesConfig(**cfg), schedule)
    return sim.run(), sim


def trace(result, txn):
    return next(t for t in result.traces if t.txn == txn)


FAULTED = config(
    routes=(route(payment_rps=7.0, capacity_rps=10.0),),
    faults=(cut(100.0, 400.0, 0.35),),
    duration=900,
)


@pytest.fixture(scope="module")
def faulted():
    return generate_workload(FAULTED)


# ============================================================ determinism and identity


def test_01_deterministic_replay(faulted):
    first, second = simulate_des(faulted), simulate_des(faulted)
    assert first == second and first.digest == second.digest


def test_02_reproducible_across_independent_processes():
    code = (
        "from intelligence.data.generate_dataset import generate_workload\n"
        "from intelligence.simulation.des_validate import simulate_des, DesConfig\n"
        "from tests.intelligence.test_des_validation import FAULTED\n"
        "print(simulate_des(generate_workload(FAULTED), DesConfig(service='EXPONENTIAL')).digest)\n"
    )
    digests = set()
    for seed in ("0", "1", "4242"):
        done = subprocess.run(  # noqa: S603 - fixed interpreter and constant code
            [sys.executable, "-c", code],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
        )
        digests.add(done.stdout.strip())
    assert len(digests) == 1


def test_03_exponential_service_is_seeded_and_isolated(faulted):
    a = simulate_des(faulted, DesConfig(service=ServiceDistribution.EXPONENTIAL, service_seed=1))
    b = simulate_des(faulted, DesConfig(service=ServiceDistribution.EXPONENTIAL, service_seed=1))
    c = simulate_des(faulted, DesConfig(service=ServiceDistribution.EXPONENTIAL, service_seed=2))
    assert a.digest == b.digest != c.digest
    random.seed(99)
    state = random.getstate()
    simulate_des(faulted, DesConfig(service=ServiceDistribution.EXPONENTIAL))
    assert random.getstate() == state  # the global random module is never used


def test_04_identical_external_arrivals_to_the_fluid_model(faulted):
    des = simulate_des(faulted)
    fluid = simulate(faulted)
    assert [i.payment_arrivals for i in des.intervals] == [
        m.payment_arrivals for m in fluid.metrics
    ]
    assert (des.run_id, des.workload_content_hash) == (
        faulted.manifest.run_id,
        faulted.manifest.content_hash,
    )


def test_05_does_not_use_the_fluid_engine():
    source = (REPO_ROOT / "intelligence/simulation/des_validate.py").read_text()
    assert "intelligence.simulation.engine" not in source
    assert "FluidSimulation" not in source


# ===================================================================== queue mechanics


def test_06_fifo_order_and_single_completion(faulted):
    result, sim = run(faulted)
    by_route: dict[str, list] = {}
    for request in sim._requests:
        by_route.setdefault(request.route, []).append(request)
    for requests in by_route.values():
        started = [r for r in requests if r.started_s is not None]
        order = sorted(started, key=lambda r: (r.enqueued_s, requests.index(r)))
        assert [r.started_s for r in order] == sorted(r.started_s for r in order)
    for request in sim._requests:
        assert not (request.finished_s is not None and request.dropped_s is not None)
        if request.finished_s is not None:
            assert request.started_s <= request.finished_s
    served = sum(1 for r in sim._requests if r.finished_s is not None)
    assert served == result.total("served")


def test_07_queues_are_never_negative_and_work_is_conserved(faulted):
    result = simulate_des(faulted, check_schedule=[CheckRelease(float(t), "A1-B", 5)
                                                   for t in range(100, 800, 10)])  # fmt: skip
    assert all(i.queue_depth >= 0 and i.bottleneck_queue_depth >= 0 for i in result.intervals)
    assert set(conservation_residuals(result).values()) == {0}


def test_08_service_never_exceeds_capacity(faulted):
    result = simulate_des(faulted)
    work = sum(10.0 if not 100 <= t < 400 else 6.5 for t in range(900))
    assert result.total("served") <= work + 1
    for interval in result.intervals:
        capacity = 6.5 if 100 <= interval.start_s < 400 else 10.0
        assert interval.served <= capacity + 1  # one request may straddle the bin edge


def test_09_zero_capacity_serves_nothing():
    scenario = config(
        routes=(route(payment_rps=2.0),), faults=(cut(0.0, 500.0, 1.0),), duration=120
    )
    result = simulate_des(generate_workload(scenario))
    assert result.total("served") == 0
    assert result.total("dropped") > 0 and result.total("timely_successes") == 0
    assert set(conservation_residuals(result).values()) == {0}


def test_10_empty_scenario_is_valid():
    result = simulate_des(manual([]))
    assert result.bins == 200 and all(v == 0 for _, v in result.totals)
    assert all(not o.newly_ambiguous and not o.resolutions for o in result.observations)


def test_11_capacity_changes_apply_on_half_open_intervals():
    b = _Bottleneck("x", ("x",), [0.0, 100.0, 200.0], [4.0, 2.0, 4.0], _rng())
    assert (b.capacity_at(99.999), b.capacity_at(100.0), b.capacity_at(200.0)) == (4.0, 2.0, 4.0)
    assert b.finish_time(99.75, 1.0) == 100.0  # 0.25 s at 4/s finishes exactly on the edge
    assert b.finish_time(99.875, 1.0) == 100.25  # 0.5 unit at 4/s, then 0.5 unit at 2/s
    assert b.finish_time(199.5, 2.0) == 200.25
    zero = _Bottleneck("z", ("z",), [0.0, 10.0], [0.0, 4.0], _rng())
    assert zero.finish_time(5.0, 1.0) == 10.25
    never = _Bottleneck("n", ("n",), [0.0], [0.0], _rng())
    assert never.finish_time(0.0, 1.0) == float("inf")


def test_12_fault_activation_is_not_off_by_one():
    scenario = config(faults=(cut(10.0, 20.0, 0.5),), duration=60)
    payments = [{"offset": 9.75, "txn": "BEFORE"}, {"offset": 10.0, "txn": "AT"},
                {"offset": 20.0, "txn": "END"}]  # fmt: skip
    _, sim = run(manual(payments, scenario))
    finished = {r.ident: r.finished_s for r in sim._requests}
    assert finished["BEFORE"] == 10.0  # 4/s before the cut
    assert finished["AT"] == 10.5  # 2/s from exactly 10.0
    assert finished["END"] == 20.25  # 4/s again from exactly 20.0


def test_13_stale_requests_drop_exactly_at_the_age_limit():
    scenario = config(faults=(cut(0.0, 100.0, 1.0),), duration=60)
    result, sim = run(manual([{"offset": 1.0}], scenario))
    (request,) = sim._requests
    assert request.dropped_s == 31.0 and request.finished_s is None
    assert [i.dropped for i in result.intervals if i.dropped] == [1]
    assert next(i.start_s for i in result.intervals if i.dropped) == 31.0


def test_14_duplicate_and_conflicting_identifiers_are_rejected():
    workload = manual([{"offset": 1.0, "txn": "DUP"}, {"offset": 2.0, "txn": "X"}])
    doubled = workload.model_copy(update={"exogenous": workload.exogenous.model_copy(
        update={"payments": workload.exogenous.payments[:1] * 2})})  # fmt: skip
    with pytest.raises(DesError, match="duplicate"):
        DiscreteEventSimulation(doubled)
    demand = CheckDemandArrival(
        experiment_id="EXP-DES",
        sequence=0,
        check_id="DUP",
        demand_id="bg",
        bank_id="A3",
        route_id="A1-B",
        offset_s=3.0,
        arrival_at=workload.manifest.config.epoch,
    )
    clash = workload.model_copy(
        update={"exogenous": workload.exogenous.model_copy(update={"check_demands": (demand,)})}
    )
    with pytest.raises(DesError, match="collides"):
        DiscreteEventSimulation(clash)
    with pytest.raises(DesError, match="unknown route"):
        DiscreteEventSimulation(workload, check_schedule=[CheckRelease(5.0, "Z9-Q", 1)])
    bad = CapacityChange.model_construct(
        **{**workload.ground_truth.capacity[0].__dict__, "capacity_rps": -1.0}
    )
    negative = workload.model_copy(update={"ground_truth": workload.ground_truth.model_copy(
        update={"capacity": (bad,)})})  # fmt: skip
    with pytest.raises(DesError, match="invalid capacity"):
        DiscreteEventSimulation(negative)
    with pytest.raises(ValueError):
        CheckRelease(float("nan"), "A1-B", 1)
    with pytest.raises(DesError, match="runs once"):
        sim = DiscreteEventSimulation(workload)
        sim.run()
        sim.run()


# ======================================================== remitter-side semantics


def test_15_timeout_boundary_is_inclusive():
    # Served 0.00-0.25 at 4/s; responses at exactly 15.0 (timely) and just after (ambiguous).
    payments = [{"offset": 0.0, "txn": "ON", "latency_ms": 14_750.0},
                {"offset": 5.0, "txn": "LATE", "latency_ms": 14_875.0}]  # fmt: skip
    result, _ = run(manual(payments))
    on, late = trace(result, "ON"), trace(result, "LATE")
    assert (on.response_s, on.ambiguous_s) == (15.0, None)
    assert late.response_s == 20.125 and late.ambiguous_s == 20.0
    assert late.resolved_how == "LATE_RESPONSE" and late.resolved_s == 20.125


def test_16_timeout_is_uncertainty_not_failure_and_credit_arrives_later():
    result, _ = run(manual([{"offset": 0.0, "latency_ms": 40_000.0}]))
    record = trace(result, "M-0")
    assert record.ambiguous_s == 15.0 and record.resolved_how == "LATE_RESPONSE"
    resolved = [r for o in result.observations for r in o.resolutions]
    assert resolved == [("M-0", "SUCCESS", "LATE_RESPONSE", 40.25)]


def test_17_lost_response_stays_unresolved_without_checks():
    result, _ = run(manual([{"offset": 0.0, "lost": True}]))
    assert result.total("new_ambiguous") == 1 and result.total("unresolved_at_end") == 1


def test_18_checks_consume_capacity_and_resolve():
    scenario = config(routes=(route(capacity_rps=1.0),), duration=200)
    lost = [{"offset": 0.25 * n, "lost": True} for n in range(4)]
    workload = manual([*lost, {"offset": 92.0, "txn": "LATE"}], scenario)
    alone, _ = run(workload)
    checked, _ = run(workload, [CheckRelease(91.0, "A1-B", 4)])
    assert trace(alone, "LATE").response_s == 93.25
    assert trace(checked, "LATE").response_s == 96.25  # behind four checks at 1 request/s
    results = [c for o in checked.observations for c in o.check_results]
    assert [r[2] for r in results] == [CheckResultKind.AUTHORITATIVE_SUCCESS.value] * 4
    assert checked.total("resolved_by_check") == 4


def test_19_check_eligibility_follows_the_timing_policy():
    workload = manual([{"offset": 0.0, "lost": True}])
    early, _ = run(workload, [CheckRelease(89.0, "A1-B", 1)])
    on_time, _ = run(workload, [CheckRelease(90.0, "A1-B", 1)])
    assert early.total("checks_released") == 0 and on_time.total("checks_released") == 1
    noncompliant, _ = run(workload, [CheckRelease(20.0, "A1-B", 1)], enforce_timing_policy=False)
    assert noncompliant.total("checks_released") == 1


def test_20_check_result_kinds_pending_and_unknown():
    slow = manual([{"offset": 0.0, "latency_ms": 200_000.0}])
    pending, _ = run(slow, [CheckRelease(91.0, "A1-B", 1)])
    assert [c[2] for o in pending.observations for c in o.check_results] == ["PENDING"]
    blocked = config(faults=(cut(0.0, 60.0, 1.0),), duration=200)
    unknown, _ = run(manual([{"offset": 0.0}], blocked), [CheckRelease(91.0, "A1-B", 1)])
    assert [c[2] for o in unknown.observations for c in o.check_results] == ["UNKNOWN"]


def test_21_check_timeout_wastes_the_check():
    blocked = config(faults=(cut(80.0, 200.0, 1.0),), duration=200)
    result, _ = run(
        manual([{"offset": 0.0, "lost": True}], blocked), [CheckRelease(91.0, "A1-B", 1)]
    )
    assert result.total("wasted_checks") == 1 and result.total("check_results") == 0
    assert [o.check_timeouts for o in result.observations if o.check_timeouts] == [("CK-M-0-1",)]


def test_22_repayment_only_when_still_unresolved_at_due():
    # Ambiguous at 15.0; late response at 45.25. Re-pay due at 45.0 fires, at 45.25 does not.
    fires = manual([{"offset": 0.0, "latency_ms": 45_000.0, "repay": True, "repay_delay": 30.0}])
    skips = manual([{"offset": 0.0, "latency_ms": 45_000.0, "repay": True, "repay_delay": 30.25}])
    fired, _ = run(fires)
    skipped, _ = run(skips)
    assert fired.total("repayment_arrivals") == 1 and trace(fired, "RP-M-0").sent_s == 45.0
    assert skipped.total("repayment_arrivals") == 0
    timely, _ = run(manual([{"offset": 0.0, "repay": True}]))
    assert timely.total("repayment_arrivals") == 0  # never ambiguous, never re-paid


def test_23_resubmission_is_idempotent():
    workload = manual([{"offset": 0.0, "latency_ms": 20_000.0, "resubmit": True,
                        "resubmit_delay": 1.0}])  # fmt: skip
    result, sim = run(workload)
    payment = sim._payments["M-0"]
    assert payment.processed_s == 20.25  # the original forwarding decided; not re-processed
    assert result.total("arrived_resubmission") == 1
    assert trace(result, "M-0").resolved_how == "LATE_RESPONSE"


def test_24_observations_carry_no_ground_truth(faulted):
    result = simulate_des(faulted, check_schedule=[CheckRelease(200.0, "A1-B", 10)])
    forbidden = {"credited", "capacity", "latency_s", "lost", "confirm", "would_repay", "work",
                 "processed_s", "queue_depth", "fault"}  # fmt: skip
    fields = set(DesObservation.__dataclass_fields__)
    assert fields.isdisjoint(forbidden)
    text = json.dumps([asdict(o) for o in result.observations[:400]])
    assert all(key not in text for key in ("credited", "capacity_rps", "latency_ms"))


def test_25_regression_capacity_overuse_and_duplicate_completion(faulted):
    result, sim = run(faulted)
    finishes = [r.finished_s for r in sim._requests if r.finished_s is not None]
    starts = sorted(r.started_s for r in sim._requests if r.started_s is not None)
    assert len(finishes) == len(set(id(r) for r in sim._requests if r.finished_s is not None))
    assert all(b >= a for a, b in zip(starts, starts[1:], strict=False))
    gaps = sorted(finishes)
    # deterministic unit work: consecutive completions are at least 1/max-capacity apart
    assert all(b - a >= 0.1 - 1e-9 for a, b in zip(gaps, gaps[1:], strict=False))
    assert result.total("served") == len(finishes)


def test_26_shared_bottleneck_couples_routes():
    two = config(routes=(route(payment_rps=4.0, capacity_rps=5.0),
                         route("A2-C", 4.0, 5.0, "A2", "C")),  # fmt: skip
                 faults=(cut(100.0, 400.0, 0.5),), duration=600)  # fmt: skip
    workload = generate_workload(two)
    alone = simulate_des(workload)
    shared = simulate_des(workload, DesConfig(bottleneck_groups=(("A1-B", "A2-C"),)))
    healthy = [i.new_ambiguous for i in alone.intervals if i.route_id == "A2-C"]
    coupled = [i.new_ambiguous for i in shared.intervals if i.route_id == "A2-C"]
    assert sum(healthy) == 0 and sum(coupled) > 0
    with pytest.raises(DesError):
        DiscreteEventSimulation(workload, DesConfig(bottleneck_groups=(("A1-B", "Q1-Z"),)))


def test_27_stale_drop_happens_before_service_resumes():
    # Capacity returns at 30.5 s: the request waiting since 0.0 s is already 30 s old (dropped),
    # the one waiting since 0.75 s is not (served).
    scenario = config(faults=(cut(0.0, 30.5, 1.0),), duration=60)
    result, sim = run(manual([{"offset": 0.0, "txn": "OLD"}, {"offset": 0.75, "txn": "YOUNG"}],
                             scenario))  # fmt: skip
    state = {r.ident: (r.dropped_s, r.finished_s) for r in sim._requests}
    assert state["OLD"] == (30.0, None)
    assert state["YOUNG"] == (None, 30.75)
    assert trace(result, "OLD").ambiguous_s == 15.0 and trace(result, "OLD").resolved_s is None


def test_28_a_timed_out_check_never_resolves_the_payment():
    # Check released at 91 s waits behind zero capacity until 101.5 s; its answer (101.87 s)
    # is later than its 10 s deadline (101 s), so it is wasted even though it was served.
    scenario = config(faults=(cut(91.0, 101.5, 1.0),), duration=200)
    workload = manual([{"offset": 0.0, "lost": True}], scenario)
    result, sim = run(workload, [CheckRelease(91.0, "A1-B", 1)])
    (check,) = [r for r in sim._requests if r.ident == "CK-M-0-1"]
    assert check.finished_s == 101.75
    assert result.total("wasted_checks") == 1 and result.total("check_results") == 0
    assert result.total("resolved_by_check") == 0 and result.total("unresolved_at_end") == 1


def test_29_a_request_cannot_complete_twice(faulted):
    _, sim = run(faulted)
    done = next(r for r in sim._requests if r.finished_s is not None)
    with pytest.raises(DesError, match="duplicate completion"):
        sim._complete(done)


def test_30_an_answer_arriving_after_the_check_deadline_is_ignored():
    # Served 100.6875-100.9375 s (inside the 101 s deadline) but answered at 101.0575 s
    # (120 ms route latency): too late, so it is wasted and must not resolve the payment.
    scenario = config(faults=(cut(91.0, 100.6875, 1.0),), duration=200)
    result, sim = run(
        manual([{"offset": 0.0, "lost": True}], scenario), [CheckRelease(91.0, "A1-B", 1)]
    )
    (check,) = [r for r in sim._requests if r.ident == "CK-M-0-1"]
    assert check.finished_s == 100.9375
    assert result.total("wasted_checks") == 1 and result.total("check_results") == 0
    assert result.total("resolved_by_check") == 0 and result.total("unresolved_at_end") == 1
