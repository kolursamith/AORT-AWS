"""Recovery strategies, and the outcome record they produce."""

from __future__ import annotations

import json
import uuid

import pytest

from aort_recovery.outcomes import build_outcome, write_outcome
from aort_recovery.strategies import build_recovery_argv, strategy_for_scenario

IDS = dict(experiment_id=str(uuid.uuid4()), injection_id=str(uuid.uuid4()))


# --- strategies -------------------------------------------------------------------

@pytest.mark.parametrize("scenario,expected", [
    ("service_stop", "restart_service"),
    ("service_pause", "unpause_service"),
    ("backup_stop", "restart_service"),
])
def test_each_failure_has_a_matching_recovery(scenario, expected):
    assert strategy_for_scenario(scenario) == expected


def test_cpu_throttle_has_no_recovery_strategy_yet():
    # Honest gap: resetting a CPU quota is not one of the three modelled
    # strategies, so it is refused rather than silently mapped to a restart.
    with pytest.raises(ValueError):
        strategy_for_scenario("cpu_throttle")


@pytest.mark.parametrize("scenario", ["", "melt_server", None])
def test_unknown_scenarios_are_refused(scenario):
    with pytest.raises(ValueError):
        strategy_for_scenario(scenario)


def test_recovery_commands_are_exact():
    assert build_recovery_argv("restart_service", "aort-fineract") == [
        "docker", "start", "aort-fineract"]
    assert build_recovery_argv("unpause_service", "aort-fineract") == [
        "docker", "unpause", "aort-fineract"]


def test_restore_from_backup_is_not_a_docker_one_liner():
    # It runs through aort_backup, so building a docker argv for it would be
    # wrong rather than merely unsupported.
    with pytest.raises(ValueError):
        build_recovery_argv("restore_from_backup", "aort-postgres")


def test_recovery_never_destroys_anything():
    for strategy in ("restart_service", "unpause_service"):
        assert build_recovery_argv(strategy, "aort-fineract")[1] not in {"rm", "kill", "down"}


# --- outcome record ----------------------------------------------------------------

def base_outcome(**overrides):
    fields = dict(
        scenario="service_pause",
        component_id="fineract",
        strategy="unpause_service",
        failure_started_at=1000.0,
        recovery_started_at=1030.0,
        recovered_at=1042.0,
        rto_seconds=42.0,
        rpo_seconds=58.0,
        availability_ratio=0.5,
        probes_total=10,
        probes_ok=5,
        transaction_integrity="balanced",
        ledger_rows_before={"m_client": 12},
        ledger_rows_after={"m_client": 12},
        notes=None,
        **IDS,
    )
    fields.update(overrides)
    return build_outcome(**fields)


def test_outcome_satisfies_the_contract(validator):
    outcome = base_outcome()
    assert not list(validator.iter_errors(outcome))
    assert outcome["recovered"] is True
    assert outcome["failure_started_at"] == "1970-01-01T00:16:40.000Z"


def test_predictions_are_left_for_the_optimizer(validator):
    outcome = base_outcome()
    assert outcome["predicted_rto_seconds"] is None
    assert outcome["predicted_rpo_seconds"] is None
    assert not list(validator.iter_errors(outcome))


def test_unrecovered_outcome_has_no_rto_and_validates(validator):
    outcome = base_outcome(recovered_at=None, rto_seconds=None)
    assert outcome["recovered"] is False
    assert not list(validator.iter_errors(outcome))


def test_an_unrecovered_outcome_may_not_claim_an_rto(validator):
    # Guards the schema itself: "never came back" must not carry a duration.
    broken = {**base_outcome(recovered_at=None, rto_seconds=None), "rto_seconds": 0.0}
    assert list(validator.iter_errors(broken))


def test_unknown_rpo_is_null_not_zero(validator):
    outcome = base_outcome(rpo_seconds=None)
    assert outcome["rpo_seconds"] is None
    assert not list(validator.iter_errors(outcome))


def test_outcomes_are_appended_as_jsonl(tmp_path, validator):
    path = tmp_path / "outcomes.jsonl"
    write_outcome(path, base_outcome())
    write_outcome(path, base_outcome(scenario="service_stop", strategy="restart_service"))
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for line in lines:
        assert not list(validator.iter_errors(json.loads(line)))


def test_availability_outside_zero_to_one_is_rejected():
    with pytest.raises(ValueError):
        base_outcome(availability_ratio=1.5)


def test_probes_ok_cannot_exceed_probes_total():
    with pytest.raises(ValueError):
        base_outcome(probes_total=3, probes_ok=4)
