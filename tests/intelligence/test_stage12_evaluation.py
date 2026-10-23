"""Stage 12: the AORTS policy adapter, the DES observation hook and the evaluation runner."""

from __future__ import annotations

import json

import pytest

from intelligence.data.generate_dataset import generate_workload
from intelligence.experiments import capacity_evaluation as ce
from intelligence.experiments import stage12_evaluation as ev
from intelligence.experiments.aorts_policy import ARMS, AortsPolicy, Impairment
from intelligence.experiments.policy_harness import run_policy
from intelligence.simulation.des_validate import DesConfig, simulate_des
from intelligence.simulation.model_selection import Model


def _small(seed=5, faults=(), duration=400.0):
    return ce._custom("t12", seed, (("A1-B", "B", 0.7, 1.0),), faults, scale=0.01,
                      duration=duration)  # fmt: skip


def _lost(seed=5):
    return _small(seed, (ce._cut("A1-B", 50.0, 400.0, 0.3, "RESPONSE_LOSS"),))


def test_des_observations_carry_own_traffic_without_changing_the_simulation():
    workload = generate_workload(_lost())
    seen = []

    def controller(now, obs, fb):
        seen.append(obs)
        return []

    closed = simulate_des(workload, DesConfig(bin_s=1.0), (), controller, 1.0)
    open_loop = simulate_des(workload, DesConfig(bin_s=1.0))
    assert closed.digest == open_loop.digest  # observation logs never alter the dynamics
    sends = sum(len(o.payments_sent) for o in seen)
    responses = [r for o in seen for r in o.payment_responses]
    assert sends == sum(1 for t in closed.traces if t.kind in ("payment", "repayment")
                        and t.sent_s < seen[-1].end_s)  # fmt: skip
    for o in seen:  # causal: nothing at or after the bin end
        assert all(o.start_s <= s[3] < o.end_s for s in o.payments_sent)
        assert all(o.start_s <= r[4] < o.end_s for r in o.payment_responses)
    assert responses and all(r[4] >= r[3] for r in responses)


def test_aorts_policy_observes_only_remitter_facts():
    config = _lost()
    policy = AortsPolicy(config)
    forbidden = ("capacity_rps", "faults", "credited", "lost", "truth")
    record = run_policy(config, "aorts", engine=Model.DES, policy=policy)
    assert record.policy_id == "aorts" and record.engine == "DES"
    assert record.metrics.noncompliant_releases == 0 and record.metrics.invalid_releases == 0
    state = json.dumps(dict(policy.state()))
    assert not any(word in state for word in forbidden)
    assert sum(policy.decisions.values()) > 0  # the real decide() ran every decision instant


@pytest.mark.parametrize("arm", sorted(ARMS))
def test_every_arm_runs_and_records_its_ablation(arm):
    config = _lost()
    policy = AortsPolicy(config, arm)
    record = run_policy(config, arm, engine=Model.DES, policy=policy)
    assert policy.state()["ablation"] == ARMS[arm][0].value
    assert record.metrics.noncompliant_releases == 0


def test_outage_uses_the_bounded_labelled_fallback():
    config = _lost()
    policy = AortsPolicy(config, impairment=Impairment(outage=(0.0, 400.0), fallback_per_route=2))
    record = run_policy(config, "aorts", engine=Model.DES, policy=policy)
    assert policy.fallback_ticks > 0
    assert sum(policy.decisions.values()) == 0  # no controller decision during the outage
    assert record.metrics.noncompliant_releases == 0


def test_poisoning_changes_only_what_aorts_observes():
    config = _lost()
    honest = run_policy(config, "no-check", engine=Model.DES)
    poisoned = AortsPolicy(config, impairment=Impairment(inflate_from_s=0.0, inflate_factor=2.0))
    run_policy(config, "aorts", engine=Model.DES, policy=poisoned)
    again = run_policy(config, "no-check", engine=Model.DES)
    assert honest.result_hash == again.result_hash  # the workload and simulator are unchanged


def test_fluid_engine_is_refused_for_observation_policies():
    config = _small()
    with pytest.raises(ValueError, match="causal remitter observations"):
        run_policy(config, "aorts", engine=Model.FLUID, dt_s=0.25, policy=AortsPolicy(config))


def test_policy_id_must_match_the_supplied_policy():
    config = _small()
    with pytest.raises(ValueError, match="does not match"):
        run_policy(config, "aimd", engine=Model.DES, policy=AortsPolicy(config))


def test_design_is_frozen_and_paired():
    assert ev.design_digest() == ev.design_digest()
    for name in ev.KEY:
        assert len(ev.seeds_for(name)) == 30
        assert set(ev.ABLATIONS) <= set(ev.arms_for(name))
    for name in ev.SCENARIOS:
        assert "no-check" in ev.arms_for(name) and "aorts" in ev.arms_for(name)
        a, b = ev.SCENARIOS[name][0](12001), ev.SCENARIOS[name][0](12001)
        assert generate_workload(a).manifest.content_hash == (
            generate_workload(b).manifest.content_hash
        )


def test_paired_statistics_are_computed_from_rows_only():
    rows = {}
    for i, seed in enumerate(ev.seeds_for("normal_load")):
        base = {k: 0 for k in ("invalid_releases", "noncompliant_releases")}
        rows[("normal_load", "no-check", seed)] = {**base, "new_ambiguous": 10}
        rows[("normal_load", "aorts", seed)] = {**base, "new_ambiguous": 10 - (i % 3)}
    p = ev.paired(rows, "normal_load", "aorts", "no-check", "new_ambiguous")
    assert p["n"] == 10 and p["mean"] == pytest.approx(-0.9)
    lo, hi = p["ci95_bootstrap"]
    assert lo <= p["mean"] <= hi
