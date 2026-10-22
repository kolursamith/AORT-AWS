"""Stage 05: causal, fair policy harness with model selection."""

import ast
import subprocess
import sys
from collections import Counter
from dataclasses import fields
from pathlib import Path

import pytest

pytest.importorskip("simpy")  # the harness runs the DES (pyproject `research` extra)

from intelligence.experiments import baselines as b  # noqa: E402
from intelligence.experiments import policy_harness as h  # noqa: E402
from intelligence.simulation import model_validation as mv  # noqa: E402
from intelligence.simulation.model_selection import Model, Purpose  # noqa: E402
from intelligence.simulation.scenarios import (  # noqa: E402
    ScenarioOptions,
    reference_scenario,
    research_timing_policy,
)

REPO = Path(__file__).resolve().parents[2]
TIMING = research_timing_policy()
STRICT_SET = ["no-check", "static-polling", "exp-backoff-jitter", "fixed-budget", "aimd"]


def ref(name, seed=3101, **options):
    return reference_scenario(name, seed=seed, experiment_id="EXP-S05T", rate_scale=0.01,
                              duration_s=2400.0, options=ScenarioOptions(**options))  # fmt: skip


def lossy(seed=3101, two_routes=False):
    """Responses lost on A1-B for 100 s: checks become eligible 90 s after sending and answer."""

    routes = (mv._route(payment_rps=5.0),)
    faults = (mv._cut(100.0, 200.0, 1.0, kind="RESPONSE_LOSS"),)
    if two_routes:
        routes += (mv._route("A2-C", 5.0, 10.0, "A2", "C"),)
        faults += (mv._cut(100.0, 160.0, 1.0, route="A2-C", kind="RESPONSE_LOSS"),)
    return mv._scenario("s05-lossy", routes, faults, duration=600.0, seed=seed)


# ------------------------------------------------------------------- timing gate


def _txn(sent=0.0, k=0, last=None):
    return h._Txn("T1", "A1-B", "A1", sent, sent + 15.0, k, last)


@pytest.mark.parametrize(
    ("now", "k", "last", "allowed"),
    [
        (89.999, 0, None, False),  # first-check delay (90 s after sending)
        (90.0, 0, None, True),
        (179.999, 1, 90.0, False),  # spacing 90 s
        (180.0, 1, 90.0, True),
        (500.0, 3, 300.0, False),  # at most 3 counted checks
        (3599.999, 2, 1000.0, True),  # inside the 3600 s research deadline
        (3600.0, 2, 1000.0, False),  # deadline reached
    ],
)
def test_timing_policy_boundaries(now, k, last, allowed):
    assert h.timing_permits(_txn(k=k, last=last), now, TIMING) is allowed


def _tracker(workload):
    tracker = h.Tracker(workload)
    first, second = (p.transaction_id for p in workload.exogenous.payments[:2])
    tracker.ambiguous(first, 20.0)
    tracker.ambiguous(second, 20.0)
    return tracker, first, second


def test_gate_refuses_duplicates_unknown_resolved_in_flight_and_rate():
    from intelligence.data.generate_dataset import generate_workload

    tracker, a, c = _tracker(generate_workload(lossy()))
    log = h.GateLog()
    now = tracker.txns[a].sent_s + 90.0
    tracker.txns[c].sent_s = tracker.txns[a].sent_s  # both eligible now
    allowed = h.gate(tracker, b.PolicyAction((a, a, "NOPE")), now, True, h.SafetyConfig(), log)
    assert allowed == {"A1-B": [a]}
    assert log.refused == Counter({"duplicate_request": 1, "not_outstanding": 1})
    tracker.released(a, "CK-x-1", now)
    tracker.resolved(c, now)
    log = h.GateLog()
    assert h.gate(tracker, b.PolicyAction((a, c)), now, True, h.SafetyConfig(), log) == {}
    assert log.refused == Counter({"in_flight": 1, "not_outstanding": 1})


def test_budget_trims_are_not_requests_and_rate_limit_is_a_refusal():
    from intelligence.data.generate_dataset import generate_workload

    tracker, a, c = _tracker(generate_workload(lossy()))
    tracker.txns[c].sent_s = tracker.txns[a].sent_s
    now = tracker.txns[a].sent_s + 90.0
    log = h.GateLog()
    action = b.PolicyAction((a, c), (("A1-B", 1),))
    assert h.gate(tracker, action, now, True, h.SafetyConfig(), log) == {"A1-B": [a]}
    assert (log.requests, log.not_selected) == (1, 1)
    log = h.GateLog()
    tight = h.SafetyConfig(max_checks_per_bank_per_decision=1)
    assert h.gate(tracker, b.PolicyAction((a, c)), now, True, tight, log) == {"A1-B": [a]}
    assert log.refused == Counter({"rate_limited": 1})


# ------------------------------------------------------------ causality and truth


class Spy:
    """Records every input; asserts causality and the absence of truth at each decision."""

    def __init__(self, inner):
        self.inner, self.info, self.inputs = inner, inner.info, []

    def decide(self, observed):
        self.inputs.append(observed)
        for v in observed.outstanding:
            assert v.ambiguous_s <= observed.now_s
            assert v.last_check_s is None or v.last_check_s <= observed.now_s
        for f in observed.feedback:
            assert f.at_s <= observed.now_s
        return self.inner.decide(observed)

    def state(self):
        return self.inner.state()


@pytest.mark.parametrize("engine", [Model.DES, Model.FLUID])
def test_feedback_is_causal_and_check_flow_is_conserved(engine, monkeypatch):
    spy = Spy(b.Aimd())
    monkeypatch.setattr(h, "make_policy", lambda pid: spy)
    record = h.run_policy(lossy(), "aimd", engine=engine, dt_s=0.25)
    released_at = {}
    for observed in spy.inputs:
        for f in observed.feedback:
            if f.kind == "RELEASED":
                released_at[f.detail] = f.at_s
                assert f.at_s < observed.now_s  # learned at a later decision
            if f.kind == "ANSWERED":
                assert f.at_s - f.latency_s < observed.now_s
    kinds = {f.kind for observed in spy.inputs for f in observed.feedback}
    assert {"RELEASED", "ANSWERED"} <= kinds  # adaptive feedback actually arrives
    m = record.metrics
    assert m.checks_released > 0 and m.resolved_by_check > 0
    assert record.harness_released == m.checks_released
    in_flight = m.checks_released - m.checks_answered - m.checks_timed_out
    assert 0 <= in_flight <= 50  # only checks released in the final check_timeout_s
    assert m.invalid_releases == 0 and m.noncompliant_releases == 0


def test_policy_inputs_carry_no_ground_truth():
    names = {f.name for t in (b.PolicyInput, b.TxnView, b.CheckOutcome) for f in fields(t)}
    forbidden = {"credited", "capacity_rps", "latency_ms", "response_lost", "would_repay",
                 "fault", "processed_s", "true_capacity"}  # fmt: skip
    assert names.isdisjoint(forbidden)


def test_controller_and_policy_layers_do_not_import_truth_or_backend():
    for rel in ("intelligence/experiments/baselines.py", "intelligence/experiments/metrics.py"):
        tree = ast.parse((REPO / rel).read_text())
        modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert not any(
            (m or "").startswith(("backend", "intelligence.simulation")) for m in modules
        )
    for path in (REPO / "intelligence/controller").glob("*.py"):
        assert "experiments" not in path.read_text()


# ------------------------------------------------------------------- model selection


def test_fluid_only_when_every_arm_is_validated():
    s1 = ref("S1")
    engine, dt, _ = h.group_engine(
        s1, ["aimd", "static-polling"], Purpose.PRIMARY, h.SafetyConfig()
    )
    assert (engine, dt) == (Model.FLUID, 1.0)
    engine, _, why = h.group_engine(s1, STRICT_SET, Purpose.PRIMARY, h.SafetyConfig())
    assert engine is Model.DES and "fixed-budget" in why and "no-check" in why
    s3 = ref("S3")
    engine, dt, _ = h.group_engine(
        s3, ["aimd", "static-polling"], Purpose.PRIMARY, h.SafetyConfig()
    )
    assert (engine, dt) == (Model.FLUID, 0.25)


def test_s2_routes_to_des_for_primary_evidence():
    engine, _, _ = h.group_engine(ref("S2"), ["aimd"], Purpose.PRIMARY, h.SafetyConfig())
    assert engine is Model.DES


def test_unknown_or_noncompliant_designs_default_to_des():
    engine, _, _ = h.group_engine(lossy(), ["aimd"], Purpose.PRIMARY, h.SafetyConfig())
    assert engine is Model.DES  # no re-payment: outside the validated designs
    engine, _, _ = h.group_engine(ref("S1"), ["naive-polling"], Purpose.EXPLORATORY,
                                  h.SafetyConfig())  # fmt: skip
    assert engine is Model.DES


def test_comparison_uses_one_engine_and_identical_workloads():
    records = h.run_comparison(ref("S1"), [*STRICT_SET, "naive-polling"])
    comparable = [r for r in records if r.category != "STRESS_UNCONSTRAINED"]
    assert len({(r.engine, r.dt_s) for r in comparable}) == 1
    assert len({r.workload_content_hash for r in records}) == 1
    stress = next(r for r in records if r.policy_id == "naive-polling")
    assert not stress.timing_compliant and stress.model_status == "UNVALIDATED"


# ----------------------------------------------------------------- fairness, order


def test_run_order_does_not_change_results():
    forward = h.run_comparison(lossy(), ["fixed-budget", "aimd"])
    backward = h.run_comparison(lossy(), ["aimd", "fixed-budget"])
    by_id = {r.policy_id: r.result_hash for r in backward}
    assert all(r.result_hash == by_id[r.policy_id] for r in forward)


def test_two_route_fairness_is_measured():
    record = h.run_policy(lossy(two_routes=True), "fixed-budget")
    ratios = dict(record.metrics.per_route_resolution)
    assert set(ratios) == {"A1-B", "A2-C"} and all(v is not None for v in ratios.values())
    assert 0.5 <= record.metrics.fairness_jain <= 1.0


def test_policies_change_only_endogenous_outcomes():
    a, c = h.run_comparison(lossy(), ["no-check", "fixed-budget"])
    assert a.workload_content_hash == c.workload_content_hash
    assert a.metrics.checks_released == 0 and c.metrics.checks_released > 0
    assert c.metrics.resolved_by_check > 0 and a.metrics.resolved_by_check == 0


def test_stress_arm_is_labelled_and_isolated():
    record = h.run_policy(lossy(), "naive-polling")
    assert record.category == "STRESS_UNCONSTRAINED" and not record.timing_compliant
    assert record.metrics.noncompliant_releases > 0 and record.metrics.invalid_releases == 0


def test_not_comparable_policies_cannot_run():
    with pytest.raises(ValueError):
        h.run_policy(lossy(), "trackb-heuristic")


# ------------------------------------------------------------------ serialization


def test_run_records_serialize_stably_across_processes():
    record = h.run_policy(lossy(), "fixed-budget")
    assert record.to_json() == h.run_policy(lossy(), "fixed-budget").to_json()
    code = (
        "from intelligence.experiments.policy_harness import run_policy\n"
        "from tests.intelligence.test_policy_harness import lossy\n"
        "print(run_policy(lossy(), 'fixed-budget').result_hash)\n"
    )
    done = subprocess.run(  # noqa: S603 - fixed interpreter and constant code
        [sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, check=True,
        env={"PYTHONHASHSEED": "7", "PATH": "/usr/bin:/bin"},
    )  # fmt: skip
    assert done.stdout.strip() == record.result_hash


def test_des_controller_observations_only_reveal_the_past():
    from intelligence.data.generate_dataset import generate_workload
    from intelligence.simulation.des_validate import simulate_des

    seen = []

    def controller(now, obs, feedback):
        assert obs.end_s == now
        for _, _, _, at in obs.resolutions:
            assert obs.start_s <= at < now
        for _, _, _, at in obs.check_results:
            assert obs.start_s <= at < now
        seen.append(obs)
        outstanding = dict(obs.ambiguous_outstanding)["A1-B"]
        return [h.CheckRequest("A1-B", outstanding[:3])] if outstanding else []

    simulate_des(generate_workload(lossy()), controller=controller)
    assert any(o.resolutions for o in seen) and any(o.check_results for o in seen)


def test_a_transaction_cannot_be_checked_at_the_instant_it_turns_ambiguous():
    from intelligence.simulation.des_validate import DesConfig, simulate_des
    from tests.intelligence.test_des_validation import manual

    results = []

    def controller(now, obs, feedback):
        if feedback is not None:
            results.append((feedback.decided_at_s, feedback.released, feedback.refused))
        return [h.CheckRequest("A1-B", ("M-0",))] if now in (15.0, 16.0) else []

    workload = manual([{"offset": 0.0, "lost": True}])  # deadline at exactly 15.0 s
    simulate_des(workload, DesConfig(enforce_timing_policy=False), controller=controller)
    by_time = {at: (rel, ref) for at, rel, ref in results}
    assert by_time[15.0] == ((), (("M-0", "not_observed_ambiguous"),))
    assert by_time[16.0][0] == (("M-0", "CK-M-0-1"),)


def test_the_shared_gate_not_the_simulator_enforces_the_timing_policy():
    record = h.run_policy(lossy(), "exp-backoff-jitter")
    m = record.metrics
    assert m.gate_refused > 0 and m.engine_refused == 0
    assert m.noncompliant_releases == 0


def test_short_fault_offers_no_eligible_checks_to_strict_policies():
    for record in h.run_comparison(ref("S3"), ["static-polling", "fixed-budget", "aimd"]):
        assert record.metrics.checks_released == 0 and record.metrics.engine_refused == 0
