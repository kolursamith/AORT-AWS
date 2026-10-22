"""Stage 06: estimator evaluation with simulated truth (group D) and the RV-1 fixture.

Truth (capacity schedule, true arrivals, true bottleneck depth) is read only by ``score_run``;
the estimator is fed Route Twin snapshots built from remitter-observable records. Tests that
run the DES need SimPy (the ``research`` extra) and skip without it.
"""

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from contracts.v1_1 import RouteTwinSnapshot
from intelligence.data.generate_dataset import generate_workload
from intelligence.experiments import capacity_evaluation as ce
from intelligence.ml.capacity_estimator import (
    CapacityEstimate,
    EstimatorMode,
    Identifiability,
    RouteState,
)
from intelligence.simulation.model_selection import (
    LoadRegime,
    Model,
    Status,
    ValidationEntry,
)
from intelligence.simulation.scenarios import reference_scenario

SMALL = dict(scale=0.02, duration=1500.0)


def design(name, seed=6001, **kw):
    return next(d for d in ce.designs(seed, **({**SMALL, **kw})) if d.name == name)


# ------------------------------------------------------------- observable projection


def test_remitter_view_carries_no_truth_fields():
    assert {f.name for f in dataclasses.fields(ce.RemitterRecord)} == {
        "route_id", "kind", "sent_s", "response_s"
    }  # fmt: skip


def test_des_telemetry_known_answers():
    epoch = datetime(2026, 1, 1, tzinfo=UTC)
    records = [
        ce.RemitterRecord("A1-B", "payment", 0.2, 0.5),  # timely
        ce.RemitterRecord("A1-B", "payment", 0.5, None),  # never answered: timeout at 15.5
        ce.RemitterRecord("A1-B", "repayment", 1.2, 20.0),  # late: timeout 16.2, reply 20.0
        ce.RemitterRecord("A1-C", "payment", 0.1, 0.3),  # other route
    ]
    out = ce.des_telemetry(records, experiment_id="EXP-T", routes=["A1-B"], epoch=epoch,
                           horizon_s=25.0, client_timeout_s=15.0)  # fmt: skip
    series = out["A1-B"]
    assert set(out) == {"A1-B"} and len(series) == 25
    w0 = series[0]
    assert (w0.payment_rps, w0.observed_throughput_rps, w0.p95_latency_ms) == (2.0, 1.0, 300.0)
    assert w0.observation_window_end == epoch + timedelta(seconds=1)
    assert w0.observation_scope == "REMITTER_OWN_TRAFFIC" and w0.check_rps == 0.0
    depth = [w.queue_depth for w in series]
    assert depth[0] == 1 and depth[1:15] == [2] * 14 and depth[15] == 1 and depth[16:] == [0] * 9
    assert (series[15].timeout_count, series[16].timeout_count) == (1, 1)
    assert series[15].timeout_rate == 1.0
    assert series[20].observed_throughput_rps == 1.0 and series[20].p95_latency_ms == 18800.0
    assert series[3].p95_latency_ms is None and series[3].timeout_rate is None
    with pytest.raises(ValueError):
        ce.des_telemetry([ce.RemitterRecord("A1-B", "resubmission", 0.0, 1.0)],
                         experiment_id="EXP-T", routes=["A1-B"], epoch=epoch, horizon_s=5.0,
                         client_timeout_s=15.0)  # fmt: skip


# ------------------------------------------------------------- truth helpers and scoring


def test_true_capacity_time_average():
    d = design("prolonged_degradation")
    workload = generate_workload(d.config)
    cap = d.config.route("A1-B").capacity_rps
    assert ce.true_capacity(workload, "A1-B", 100.0, 101.0) == pytest.approx(cap)
    assert ce.true_capacity(workload, "A1-B", 590.0, 610.0) == pytest.approx((cap + 0.65 * cap) / 2)


def _estimate(t, **kw):
    base = dict(
        config_digest="x" * 64, experiment_id="EXP-S06", route_id="A1-B",
        decision_time=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=t),
        snapshot_version=1, observation_watermark=(), last_observation_at=None,
        observation_age_s=0.0, twin_freshness="FRESH", mode=EstimatorMode.TRUSTED,
        route_state=RouteState.HEALTHY, identifiability=Identifiability.LOWER_BOUND_ONLY,
        observed_offered_rps=7.0, observed_throughput_rps=7.0, offered_load_rps=7.0,
        estimated_capacity_rps=None, capacity_lower_bound_rps=None, utilization_estimate=None,
        utilization_upper_bound=None, safe_headroom_rps=0.0, confidence=1.0, usable_samples=10,
        saturation_samples=0, rejected_samples=0, reason_codes=(),
    )  # fmt: skip
    return CapacityEstimate(**{**base, **kw})


def test_score_run_known_answers_and_undefined_metrics():
    d = ce.Design("probe", "constant capacity 10", ce._custom("probe", 1, (("A1-B", "B", 0.7,
                  1.0),), scale=0.01, duration=60.0), ("A1-B",))  # fmt: skip
    workload = generate_workload(d.config)
    est = [
        _estimate(1, identifiability=Identifiability.IDENTIFIED, estimated_capacity_rps=12.0,
                  capacity_lower_bound_rps=11.0),  # +2 error, unsafe, bound violation
        _estimate(2, identifiability=Identifiability.IDENTIFIED, estimated_capacity_rps=9.0,
                  capacity_lower_bound_rps=8.0),  # -1 error
        _estimate(3, capacity_lower_bound_rps=9.0, safe_headroom_rps=1.0),  # 7 + 1 <= 10
        _estimate(4, capacity_lower_bound_rps=9.0, safe_headroom_rps=1.5),  # 9.5 + 1.5 > 10
        _estimate(5, mode=EstimatorMode.STALE),
    ]  # fmt: skip
    decisions = [ce.Decision(float(i + 1), "A1-B", e, True, False, 0.0)
                 for i, e in enumerate(est)]  # fmt: skip
    offered = {"A1-B": [7.0, 7.0, 7.0, 20.0, 7.0]}  # 10 s means: 7, 7, 7, 10.25, 9.6
    depth = {"A1-B": [5, 5, 0, 0, 0]}  # busy at decision 2 only
    s = ce.score_run(workload, d, decisions, offered, depth)["A1-B"]["periods"]["all"]
    assert s["point_decisions"] == 2 and s["mae_rps"] == pytest.approx(1.5)
    assert s["rmse_rps"] == pytest.approx((2.5) ** 0.5)
    assert s["mean_rel_error"] == pytest.approx(0.15)
    assert s["unsafe_point_rate"] == pytest.approx(0.5)
    assert s["bound_decisions"] == 4 and s["bound_violations"] == 1
    assert s["bound_coverage"] == pytest.approx(0.75)
    assert s["headroom_positive"] == 2 and s["headroom_overestimates"] == 1
    assert s["insufficient_fraction"] == pytest.approx(0.2)
    assert s["false_identified"] == 1  # the first point came before any busy window
    empty = ce.PeriodScore().summary()
    assert empty["mae_rps"] is None and empty["bound_coverage"] is None
    assert empty["unsafe_point_rate"] is None and empty["headroom_overestimate_rate"] is None


# ------------------------------------------------------------- unchanged reference designs


def test_reference_scenarios_are_unchanged_and_reused():
    s2 = reference_scenario("S2", seed=1, experiment_id="E", rate_scale=0.05, duration_s=2400.0)
    s3 = reference_scenario("S3", seed=1, experiment_id="E", rate_scale=0.05, duration_s=2400.0)
    s1 = reference_scenario("S1", seed=1, experiment_id="E", rate_scale=0.05, duration_s=2400.0)
    assert [(f.start_s, f.end_s, f.magnitude) for f in s2.faults] == [(600.0, 1800.0, 0.35)]
    assert [(f.start_s, f.end_s, f.magnitude) for f in s3.faults] == [(600.0, 720.0, 0.35)]
    assert s1.faults == () and s1.route("A1-B").payment_rps == pytest.approx(35.0)
    assert s1.route("A1-B").capacity_rps == pytest.approx(50.0)
    by_name = {d.name: d for d in ce.designs(1)}
    assert by_name["prolonged_degradation"].config == reference_scenario(
        "S2", seed=1, experiment_id="EXP-S06", rate_scale=0.05, duration_s=2400.0
    )
    assert len(by_name) == 17 and len(set(by_name)) == 17


def test_trackb_estimator_is_documented_as_not_comparable():
    assert ce.TRACKB_ESTIMATOR["status"] == "NOT_COMPARABLE"
    assert ce.trackb_inputs_missing() == ("workers", "samples")


def test_engine_choice_follows_stage04s_selector():
    for d in ce.designs(6001):
        sel = ce.choose_engine(d.config)
        assert sel.model is Model.DES  # passive designs carry no validated fluid regime
    permissive = (ValidationEntry(LoadRegime.LOW, True, False, 1.0, Status.GREEN, "test"),)
    low = design("normal_underutilized")
    assert ce.choose_engine(low.config, permissive).model is Model.FLUID
    s2 = design("prolonged_degradation")
    assert ce.choose_engine(s2.config, permissive).model is Model.DES  # STALE regime


# ------------------------------------------------------------- simulation-backed (SimPy)


@pytest.fixture(scope="module")
def simpy_available():
    pytest.importorskip("simpy")


@pytest.fixture(scope="module")
def s2_record(simpy_available):
    return ce.evaluate_design(design("prolonged_degradation", duration=2400.0))


def test_s2_primary_evaluation_uses_des_and_tracks_the_drop(s2_record):
    r = s2_record
    assert r.engine == "DES" and r.model_status == "UNVALIDATED"
    s = r.scores["A1-B"]
    assert s["congestion_detection_delay_s"] is not None
    assert s["congestion_detection_delay_s"] <= 60.0
    c = s["periods"]["C_sustained"]
    assert c["point_decisions"] > 0.5 * c["decisions"]
    assert c["mae_rps"] <= 0.05 * s["true_capacity_degraded_rps"]
    assert c["bound_coverage"] >= 0.95 and c["headroom_positive"] == 0
    # Known exposure: until the drop is detected, headroom granted on pre-drop evidence can
    # exceed the new capacity. It must stay confined to the onset period.
    periods = s["periods"]
    for name in ("A_before", "C_sustained", "D_after_recovery"):
        assert periods[name]["headroom_overestimates"] == 0, name
    assert periods["B_onset"]["headroom_overestimates"] <= s["congestion_detection_delay_s"]
    assert s["conservation_fail_closed"] == 0  # no false integrity alarm on honest data


def test_matched_seeds_and_deterministic_records(simpy_available, s2_record):
    again = ce.evaluate_design(design("prolonged_degradation", duration=2400.0))
    assert again.result_hash == s2_record.result_hash
    assert again.workload_content_hash == s2_record.workload_content_hash
    other = design("prolonged_degradation", seed=6002, duration=2400.0)
    assert generate_workload(other.config).manifest.content_hash != again.workload_content_hash


def test_low_load_is_reported_unidentifiable_not_estimated(simpy_available):
    r = ce.evaluate_design(design("normal_underutilized"))
    s = r.scores["A1-B"]
    assert s["identifiability"].get("IDENTIFIED", 0) == 0
    assert s["periods"]["all"]["bound_coverage"] == 1.0
    assert s["periods"]["all"]["point_decisions"] == 0


def test_estimator_receives_only_twin_snapshots(simpy_available, monkeypatch):
    seen = []
    original = ce.CapacityEstimator.update

    def spy(self, snapshot, decision_time):
        seen.append((type(snapshot), type(decision_time)))
        return original(self, snapshot, decision_time)

    monkeypatch.setattr(ce.CapacityEstimator, "update", spy)
    ce.evaluate_design(design("fault_recovery", duration=900.0))
    assert seen and set(seen) == {(RouteTwinSnapshot, datetime)}


def test_des_telemetry_is_causal(simpy_available):
    from intelligence.simulation.des_validate import DesConfig, simulate_des

    d = design("prolonged_degradation", duration=900.0)
    workload = generate_workload(d.config)

    def telemetry(horizon):
        result = simulate_des(workload, DesConfig(bin_s=1.0, horizon_s=horizon))
        return ce.des_telemetry(ce.remitter_view(result), experiment_id=d.config.experiment_id,
                                routes=["A1-B"], epoch=d.config.epoch, horizon_s=horizon,
                                client_timeout_s=15.0)["A1-B"]  # fmt: skip

    short, long = telemetry(700.0), telemetry(900.0)
    assert short == long[: len(short)]  # later events never change earlier windows


def test_fluid_path_runs_when_the_table_permits(simpy_available):
    permissive = (ValidationEntry(LoadRegime.LOW, True, False, 1.0, Status.GREEN, "test"),)
    r = ce.evaluate_design(design("normal_underutilized", duration=900.0), table=permissive)
    assert r.engine == "FLUID" and r.dt_s == 1.0
    assert r.scores["A1-B"]["periods"]["all"]["bound_coverage"] == 1.0


def test_subtle_poisoning_is_detected_by_conservation_on_simulation(simpy_available):
    r = ce.evaluate_design(design("subtle_poisoning"))
    s = r.scores["A1-B"]
    assert s["first_conservation_detection_s"] is not None
    assert 1200.0 <= s["first_conservation_detection_s"] <= 1260.0
    assert s["periods"]["E_poisoned"]["headroom_overestimates"] == 0


def test_parallel_routes_are_isolated(simpy_available):
    r = ce.evaluate_design(design("healthy_parallel_route", duration=900.0))
    assert r.isolation_errors == 0 and set(r.scores) == {"A1-B", "A1-C"}
    assert r.scores["A1-C"]["congestion_detection_delay_s"] is None


# ------------------------------------------------------------- recovery-valuable RV-1


def test_rv1_spec_is_labelled_preparation_only():
    assert "not used for any AORTS improvement claim" in ce.RECOVERY_VALUABLE_SPEC["label"]
    config = ce.recovery_valuable_design(seed=7001)
    kinds = sorted(f.kind for f in config.faults)
    assert kinds == ["CAPACITY_CUT", "RESPONSE_LOSS"]
    assert config.scenario_id == "rv1-commit-then-lose"


def test_rv1_checks_can_answer_committed_payments_under_contention(simpy_available):
    from intelligence.simulation.des_validate import CheckRelease, DesConfig, simulate_des

    config = ce.recovery_valuable_design(seed=7001)
    workload = generate_workload(config)
    committed_lost = [
        b for b in workload.ground_truth.beneficiary if b.credited and b.response_lost
    ]
    assert committed_lost  # committed by the beneficiary, confirmation never delivered
    schedule = tuple(CheckRelease(float(t), "A1-B", 5) for t in range(400, 1300, 30))
    with_checks = simulate_des(workload, DesConfig(bin_s=1.0), schedule)
    without = simulate_des(workload, DesConfig(bin_s=1.0))
    assert with_checks.total("resolved_by_check") > 0  # useful recovery exists
    assert with_checks.total("completed_status_check") > 0  # checks use the bottleneck
    depth = sum(i.bottleneck_queue_depth for i in with_checks.intervals)
    assert depth >= sum(i.bottleneck_queue_depth for i in without.intervals)
