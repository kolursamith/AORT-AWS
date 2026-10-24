"""Stage 04R: regression tests for the Stage 3B stale-drop / in-service accounting repair.

Rule under test (shared with the independent discrete-event model): a request still *waiting*
may be dropped once it is stale; a request already *in service* (the partially served head)
stays in service and keeps the service it has received until it completes.

Capacities are binary fractions (1/32, 1/4, ...) so progress sums are exact.
"""

import pytest

from intelligence.data.generate_dataset import generate_workload
from intelligence.simulation.engine import (
    CheckDecision,
    FluidSimulation,
    SimulationAccountingError,
    SimulationConfig,
    simulate,
)
from intelligence.simulation.scenarios import RepaySpec
from tests.intelligence.test_fluid_simulation import (
    FAULTED,
    config,
    cut,
    flood,
    manual,
    route,
)

CLASSES = ("payment", "repayment", "resubmission", "status_check", "background_check")


def slow(capacity: float, duration: float = 120.0, faults=()):
    return config(routes=(route(capacity_rps=capacity),), duration=duration, faults=faults)


# 7 requests/s against 10, cut by 35 % for 600 s: queueing delay passes 30 s, so stale drops
# happen while the route runs at a fractional capacity (6.5 requests/s).
LONG_FAULT = config(
    routes=(route(payment_rps=7.0),), faults=(cut("A1-B", 100.0, 700.0),), duration=1200
)


def stepped(workload, **cfg):
    """Run step by step, checking every invariant after every interval."""

    sim = FluidSimulation(workload, SimulationConfig(**cfg))
    ledgers = []
    while not sim.done:
        sim.step()
        sim.check_invariants()
        ledgers.append(sim.service_accounting())
    return sim, ledgers


def by_start(result):
    return {m.start_s: m for m in result.metrics}


def response_time(observations, txn):
    return next(
        (o.observed_at for obs in observations for o in obs.outcomes if o.transaction_id == txn),
        None,
    )


# --------------------------------------------------------------------------- R01


def test_r01_partially_served_request_survives_the_stale_age_and_completes():
    # 1/32 request/s: the request needs 32 intervals; its cohort turns stale (age > 30 s) at
    # t = 31 while it holds 31/32 of its service. The old engine dropped it and the 31/32 unit.
    sim, ledgers = stepped(manual([{"offset": 0.0, "txn": "SLOW"}], slow(1 / 32)))
    result = sim.result()
    assert result.total("dropped") == 0 and result.total("served") == 1
    assert by_start(result)[31.0].served == 1  # completes at the end of interval 31 (t = 32)
    _, offered, delivered, completed, progress = ledgers[30][0]  # after interval 30
    assert (delivered, completed, progress) == (31 / 32, 0, 31 / 32)
    assert ledgers[-1][0][2:4] == (1.0, 1)


# --------------------------------------------------------------------------- R02


def test_r02_waiting_requests_behind_the_one_in_service_still_expire():
    payments = [
        {"offset": 0.0, "txn": "HEAD"},  # in service from t = 0
        {"offset": 0.5, "txn": "WAIT"},  # same cohort, waiting behind HEAD
        {"offset": 10.0, "txn": "LATER"},  # in service from t = 32, so never dropped
    ]
    sim, _ = stepped(manual(payments, slow(1 / 32)))
    result = sim.result()
    dropped = [m.start_s for m in result.metrics if m.dropped]
    assert dropped == [31.0]  # WAIT: its cohort is older than 30 s at the start of t = 31
    assert result.total("served") == 2 and result.total("queued_at_end") == 0
    assert by_start(result)[63.0].served == 1  # LATER finishes after 32 more intervals


# --------------------------------------------------------------------------- R03


def test_r03_no_service_accumulates_while_capacity_is_zero():
    scenario = slow(1 / 4, faults=(cut("A1-B", 0.0, 50.0, 1.0),))
    sim, ledgers = stepped(manual([{"offset": 0.0}], scenario))
    assert all(ledger[0][2] == 0.0 and ledger[0][4] == 0.0 for ledger in ledgers[:50])
    result = sim.result()
    assert result.total("dropped") == 1 and result.total("served") == 0
    assert [m.start_s for m in result.metrics if m.dropped] == [31.0]  # waiting, so it expires


# --------------------------------------------------------------------------- R04


def test_r04_capacity_change_during_service_keeps_received_service():
    # 1/4 per s, halved from t = 2.5: the interval [2, 3) averages 0.1875.
    scenario = slow(1 / 4, faults=(cut("A1-B", 2.5, 100.0, 0.5),))
    sim, ledgers = stepped(manual([{"offset": 0.0}], scenario))
    progress = [ledger[0][4] for ledger in ledgers[:6]]
    assert progress == [0.25, 0.5, 0.6875, 0.8125, 0.9375, 0.0]
    assert by_start(sim.result())[5.0].served == 1


def test_r04b_service_resumes_after_a_zero_capacity_gap_with_progress_intact():
    scenario = slow(1 / 4, duration=200, faults=(cut("A1-B", 2.0, 60.0, 1.0),))
    sim, ledgers = stepped(manual([{"offset": 0.0}], scenario))
    assert ledgers[1][0][4] == 0.5 and ledgers[59][0][4] == 0.5  # held through the outage
    result = sim.result()
    assert result.total("dropped") == 0 and by_start(result)[61.0].served == 1


# --------------------------------------------------------------------------- R05


def test_r05_work_and_service_are_conserved_every_interval():
    workload = generate_workload(FAULTED)
    sim = FluidSimulation(workload, SimulationConfig(enforce_timing_policy=False))
    while not sim.done:
        sim.step(flood(sim.last_observation))
        sim.check_invariants()  # per-class arrivals and the service ledger
        for _, offered, delivered, completed, progress in sim.service_accounting():
            assert delivered <= offered + 1e-6
            assert delivered == pytest.approx(completed + progress, abs=1e-6)
            assert 0.0 <= progress < 1.0
    result = sim.result()
    assert result.total("dropped") > 1000  # the stale-drop path is exercised
    completed = sum(result.total(f"completed_{c}") for c in CLASSES)
    assert completed == result.total("served") == sim.service_accounting()[0][3]


def test_r05b_the_ledger_detects_destroyed_service():
    sim = FluidSimulation(manual([{"offset": 0.0}], slow(1 / 32)))
    for _ in range(5):
        sim.step()
    (bottleneck,) = sim._bottlenecks.values()
    bottleneck.head_progress = 0.0  # what the pre-repair stale drop did
    with pytest.raises(SimulationAccountingError, match="service delivered"):
        sim.check_invariants()


# --------------------------------------------------------------------------- R06


def test_r06_same_instant_boundaries_are_deterministic():
    """At t = 31: STALE expires, capacity returns, NEW arrives; EARLY was enqueued at 30.5.

    Documented order inside an interval: (1) waiting stale cohorts are dropped, (2) releases,
    customer actions and arrivals are enqueued in time order, (3) service uses the capacity
    averaged over [t, t + dt), (4) completions happen at t + dt. So STALE is dropped even
    though capacity returns at the same instant, and EARLY is served before NEW.
    """

    scenario = slow(4.0, faults=(cut("A1-B", 0.0, 31.0, 1.0),))
    payments = [
        {"offset": 0.0, "txn": "STALE"},
        {"offset": 30.5, "txn": "EARLY"},
        {"offset": 31.0, "txn": "NEW"},
    ]
    runs = []
    for _ in range(2):
        sim = FluidSimulation(manual(payments, scenario))
        observations = [sim.step() for _ in range(40)]
        runs.append((sim.result(), observations))
    (result, observations), (again, _) = runs
    assert result == again
    tick31 = by_start(result)[31.0]
    assert (tick31.dropped, tick31.served, tick31.capacity_rps) == (1, 2, 4.0)
    early, new = response_time(observations, "EARLY"), response_time(observations, "NEW")
    assert early == new and early is not None  # both complete at t = 32
    outcomes = [o.transaction_id for obs in observations for o in obs.outcomes]
    assert outcomes.index("EARLY") < outcomes.index("NEW")
    assert response_time(observations, "STALE") is None


# --------------------------------------------------------------------------- R07


@pytest.mark.parametrize("dt", [1.0, 0.5, 0.25, 0.1])
def test_r07_every_timestep_conserves_work_and_uses_full_capacity(dt):
    workload = generate_workload(LONG_FAULT)
    sim, _ = stepped(workload, dt_s=dt)
    result = sim.result()
    rows = result.metrics
    drop_ticks = [m.tick for m in rows if m.dropped]
    assert drop_ticks, "the case must exercise stale drops"
    window = range(min(drop_ticks), max(drop_ticks) + 1)
    work = sum(rows[t].capacity_rps * dt for t in window)
    served = sum(rows[t].served for t in window)
    assert work - 1.0 <= served <= work + 1.0  # no service is lost when cohorts are dropped
    assert sum(result.total(f"arrived_{c}") for c in CLASSES) == (
        result.total("served") + result.total("dropped") + result.total("queued_at_end")
    )


def test_r07b_drops_do_not_grow_as_the_timestep_shrinks():
    """Pre-repair, unresolved backlog grew from 544 to 745 as dt went 1.0 -> 0.1 (service
    destroyed once per interval). After the repair it must not grow with resolution."""

    scenario = FAULTED.model_copy(update={"repay": RepaySpec(probability=1.0)})
    workload = generate_workload(scenario)
    dropped = {dt: simulate(workload, SimulationConfig(dt_s=dt)).total("dropped")
               for dt in (1.0, 0.5, 0.25, 0.1)}  # fmt: skip
    assert dropped[0.1] <= dropped[1.0]
    assert max(dropped.values()) - min(dropped.values()) <= 0.25 * dropped[1.0]


# --------------------------------------------------------------------------- R08


def test_r08_a_kept_in_service_request_completes_exactly_once():
    sim, ledgers = stepped(manual([{"offset": 0.0, "txn": "SLOW"}], slow(1 / 32)))
    result = sim.result()
    assert result.total("served") == 1 and ledgers[-1][0][3] == 1
    assert result.total("timely_successes") + result.total("late_responses") == 1
    assert sum(m.served for m in result.metrics) == 1


# --------------------------------------------------------------------------- R09


def test_r09_class_accounting_with_checks_repays_and_resubmissions():
    scenario = FAULTED.model_copy(update={"repay": RepaySpec(probability=0.5)})
    payments = [{"offset": 0.2, "lost": True, "resubmit": True, "resubmit_delay": 1.0}]
    workload = generate_workload(scenario)
    sim = FluidSimulation(workload, SimulationConfig(enforce_timing_policy=False))
    while not sim.done:
        decisions = flood(sim.last_observation)
        sim.step(decisions)
        sim.check_invariants()
    result = sim.result()
    for cls in CLASSES:
        arrived = result.total(f"arrived_{cls}")
        ended = result.total(f"completed_{cls}") + result.total(f"dropped_{cls}")
        assert ended <= arrived
    assert result.total("arrived_repayment") > 0 and result.total("dropped_status_check") > 0
    # a status check in service is kept like any other request
    check_case = manual(payments, slow(1 / 32, duration=300))
    sim = FluidSimulation(check_case, SimulationConfig(enforce_timing_policy=False))
    while not sim.done:
        sim.step([CheckDecision(route_id="A1-B", count=1)] if sim.tick == 20 else [])
        sim.check_invariants()
    totals = sim.result()
    assert totals.total("arrived_status_check") == 1
    assert totals.total("arrived_resubmission") == 1


# --------------------------------------------------------------------------- R10


# Golden digests per platform math library. Workload generation and the fluid engine use libm
# (exp/log); macOS libm and glibc differ in the last bits, so a digest is only reproducible on
# the same library. The Darwin values are the Stage 3B digests recorded before the R10 repair
# (the "unchanged by the repair" evidence); the glibc values pin the repaired engine on Linux
# (CI) as a regression guard. Bits differ, conclusions (no drops) do not.
R10_DIGESTS = {
    "S1": {
        "Darwin": "ef8c79e98eb62824bf3173196e14fa7897247f68f15477029bb5321c7ec0fcc7",
        "Linux": "e546ffa7dce0ac225c5c146e42ce48da2498d85a83d82b72551872b2067416ac",
    },
    "S3": {
        "Darwin": "dd55abe80415ced8ddc0fb71271e0fa8e4a03f4e0700cdbeadf4cfbc2e3ac1b8",
        "Linux": "c86607e7a78a01a44cf7883f4b9e4a3549175acca6d95728c34d5c298d664220",
    },
}


@pytest.mark.parametrize("name", ["S1", "S3"])
def test_r10_runs_without_stale_drops_are_unchanged_by_the_repair(name):
    """S1/S3 (seed 11, 1/100 scale, 2400 s) never drop a request; their digests hold."""

    import platform

    from intelligence.simulation.scenarios import reference_scenario

    workload = generate_workload(
        reference_scenario(name, seed=11, experiment_id="EXP-3B", rate_scale=0.01,
                           duration_s=2400.0)
    )  # fmt: skip
    result = simulate(workload)
    assert result.total("dropped") == 0
    expected = R10_DIGESTS[name].get(platform.system())
    if expected is None:  # no recorded digest for this libm: deterministic rerun instead
        assert simulate(workload).digest == result.digest
    else:
        assert result.digest == expected
