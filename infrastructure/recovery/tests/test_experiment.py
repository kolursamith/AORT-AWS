"""Orchestration: fail, observe, recover, measure - and always restore."""

from __future__ import annotations

import json

import pytest

from aort_recovery.experiment import RecoveryError, run_recovery_experiment


def run(fakes, tmp_path, probe_sequence, *, scenario="service_pause",
        component_id="fineract", docker=None, ledger=None, backup_at=940.0, **kwargs):
    options = dict(
        docker=docker or fakes["docker"](),
        scenario=scenario,
        component_id=component_id,
        probe=fakes["probe"](probe_sequence),
        ledger=ledger or fakes["ledger"](),
        backup_at=backup_at,
        failure_seconds=3,
        probe_interval=1,
        recovery_timeout=10,
        outcomes_path=tmp_path / "outcomes.jsonl",
        events_path=tmp_path / "events.jsonl",
        sleep=lambda _s: None,
        now=fakes["clock"](),
    )
    options.update(kwargs)
    return run_recovery_experiment(**options)


def test_experiment_injects_then_recovers(fakes, tmp_path):
    docker = fakes["docker"]()
    outcome = run(fakes, tmp_path, [False, False, False, True, True], docker=docker)
    assert docker.commands == ["inspect", "pause", "unpause"]
    assert outcome["recovered"] is True
    assert outcome["strategy"] == "unpause_service"


def test_measured_values_land_in_the_outcome(fakes, tmp_path, validator):
    outcome = run(fakes, tmp_path, [False, False, False, True, True])
    assert outcome["rto_seconds"] is not None and outcome["rto_seconds"] > 0
    assert 0.0 < outcome["availability_ratio"] < 1.0
    assert outcome["probes_ok"] < outcome["probes_total"]
    assert outcome["transaction_integrity"] == "balanced"
    assert not list(validator.iter_errors(outcome))


def test_outcome_is_written_as_jsonl(fakes, tmp_path, validator):
    run(fakes, tmp_path, [False, True, True])
    lines = (tmp_path / "outcomes.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert not list(validator.iter_errors(json.loads(lines[0])))


def test_injection_events_are_recorded_for_the_label_trail(fakes, tmp_path):
    run(fakes, tmp_path, [False, True, True])
    events = [json.loads(line) for line
              in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["phase"] for e in events] == ["start", "end"]
    assert len({e["injection_id"] for e in events}) == 1


def test_outcome_links_to_its_injection(fakes, tmp_path):
    outcome = run(fakes, tmp_path, [False, True, True])
    events = [json.loads(line) for line
              in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert outcome["injection_id"] == events[0]["injection_id"]


def test_a_service_that_never_returns_is_reported_honestly(fakes, tmp_path, validator):
    outcome = run(fakes, tmp_path, [False] * 40)
    assert outcome["recovered"] is False
    assert outcome["rto_seconds"] is None and outcome["recovered_at"] is None
    assert outcome["availability_ratio"] == 0.0
    assert not list(validator.iter_errors(outcome))


def test_rpo_is_unknown_without_a_backup(fakes, tmp_path, validator):
    outcome = run(fakes, tmp_path, [False, True, True], backup_at=None)
    assert outcome["rpo_seconds"] is None
    assert not list(validator.iter_errors(outcome))


def test_rpo_is_measured_from_the_newest_backup(fakes, tmp_path):
    # Clock starts at 1000 and the failure is injected on the first read.
    outcome = run(fakes, tmp_path, [False, True, True], backup_at=940.0)
    assert outcome["rpo_seconds"] == pytest.approx(60.0, abs=5)


def test_an_unbalanced_ledger_after_recovery_is_reported(fakes, tmp_path, validator):
    ledger = fakes["ledger"](debits=1500.0, credits=1400.0)
    outcome = run(fakes, tmp_path, [False, True, True], ledger=ledger)
    assert outcome["transaction_integrity"] == "unbalanced"
    assert not list(validator.iter_errors(outcome))


def test_row_counts_are_captured_before_and_after(fakes, tmp_path):
    ledger = fakes["ledger"](counts=[{"m_client": 12}, {"m_client": 11}])
    outcome = run(fakes, tmp_path, [False, True, True], ledger=ledger)
    assert outcome["ledger_rows_before"] == {"m_client": 12}
    assert outcome["ledger_rows_after"] == {"m_client": 11}


# --- refusals and failures ----------------------------------------------------------

def test_cpu_throttle_is_refused_before_anything_is_touched(fakes, tmp_path):
    docker = fakes["docker"]()
    with pytest.raises(ValueError):
        run(fakes, tmp_path, [True], scenario="cpu_throttle", docker=docker)
    assert docker.commands == []


def test_unknown_component_is_refused(fakes, tmp_path):
    with pytest.raises(ValueError):
        run(fakes, tmp_path, [True], component_id="grafana")


def test_a_target_that_is_already_down_is_refused(fakes, tmp_path):
    docker = fakes["docker"](state="exited")
    with pytest.raises(RecoveryError, match="not running"):
        run(fakes, tmp_path, [True], docker=docker)


def test_a_failed_recovery_command_is_loud(fakes, tmp_path):
    docker = fakes["docker"](results=[(0, "running", ""), (0, "", ""), (1, "", "daemon gone")])
    with pytest.raises(RecoveryError, match="daemon gone"):
        run(fakes, tmp_path, [False, True, True], docker=docker)


def test_recovery_is_attempted_even_if_probing_explodes(fakes, tmp_path):
    def exploding_probe():
        raise RuntimeError("probe backend down")

    docker = fakes["docker"]()
    with pytest.raises(RuntimeError):
        run(fakes, tmp_path, [True], docker=docker, probe=exploding_probe)
    assert "unpause" in docker.commands, "the stack was left paused"
