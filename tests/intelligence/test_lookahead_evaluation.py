"""Stage 07: the forecast-validation harness (matched DES runs, truth only in scoring)."""

from datetime import timedelta

import pytest

from intelligence.data.generate_dataset import generate_workload
from intelligence.experiments import capacity_evaluation as ce
from intelligence.experiments import lookahead_evaluation as le
from intelligence.simulation.scenarios import reference_scenario

pytest.importorskip("simpy")


def case(name, seed=7101):
    return next(c for c in le.cases(seed) if c.name == name)


def test_cases_cover_the_required_designs_and_keep_s2_unchanged():
    names = [c.name for c in le.cases(7101)]
    assert names == [
        "no_load",
        "low_load",
        "near_saturation",
        "prolonged_capacity_loss",
        "status_check_burst",
        "repayment_feedback",
        "lost_confirmation_recovery",
        "stale_telemetry",
        "poisoning_flag",
        "sudden_capacity_drop",
        "healthy_parallel_route",
    ]
    s2 = case("prolonged_capacity_loss").config
    assert s2 == reference_scenario(
        "S2", seed=7101, experiment_id="EXP-S07", rate_scale=le.SCALE, duration_s=1500.0
    )


def test_observation_never_reads_truth(monkeypatch):
    def forbidden(*_a, **_k):
        raise AssertionError("observation must not read the capacity truth")

    c = case("prolonged_capacity_loss")
    workload = generate_workload(c.config)
    monkeypatch.setattr(ce, "true_capacity", forbidden)
    state = le.observe(c, workload)
    fi = state.forecast_input
    assert fi.decision_time == c.config.epoch + timedelta(seconds=c.decision_s)
    assert fi.capacity.decision_time <= fi.decision_time
    assert all(cand.route_id == c.route_id for cand in fi.candidates)


def test_matched_runs_share_the_prefix():
    c = case("lost_confirmation_recovery")
    workload = generate_workload(c.config)
    a = le.actual_outcome(c, workload, 0)
    again = le.actual_outcome(c, workload, 0)
    assert a == again
    big = le.actual_outcome(c, workload, 8)
    assert big.checks_released > 0 and a.checks_released == 0


def test_saturated_route_forecast_tracks_the_des_and_is_deterministic():
    c = case("prolonged_capacity_loss")
    record = le.evaluate_case(c)
    assert record.engine == "DES" and record.model_selection.startswith("DES/")
    full = record.full
    assert full["available"]
    assert full["direction_agreement"]["queue"] == 1.0
    for row in full["rows"]:  # checks add load and do not raise the predicted check payoff here
        assert row["pred_checks"] == pytest.approx(row["act_checks"])
    assert le.evaluate_case(c).result_hash == record.result_hash


def test_stale_telemetry_yields_no_forecast():
    record = le.evaluate_case(case("stale_telemetry"))
    assert record.full["available"] is False
    assert "CAPACITY_STALE" in record.full["reasons"]


def test_flagged_telemetry_propagates_as_degraded_trust():
    record = le.evaluate_case(case("poisoning_flag"))
    assert record.estimator_mode == "DEGRADED"
    assert {"TRUST_DEGRADED", "FLAGGED_TELEMETRY"} <= set(record.full["reasons"])


def test_comparison_helpers():
    assert le.kendall([1.0, 2.0, 3.0], [10.0, 20.0, 30.0]) == 1.0
    assert le.kendall([1.0, 2.0, 3.0], [30.0, 20.0, 10.0]) == 0.0
    assert le.kendall([5.0, 5.0], [7.0, 7.0]) is None
    assert le._sign(0.4) == 0 and le._sign(3.0) == 1 and le._sign(-3.0, 100.0) == -1
    assert le._sign(1.5, 100.0) == 0  # below 2 % of the zero-budget value
