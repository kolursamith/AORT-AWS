"""Bug-hunt pass: the ways an injection could leave the stack broken."""

from __future__ import annotations

import pytest

from aort_injection import __main__ as cli
from aort_injection import session as session_module
from aort_injection.session import InjectionError, run_injection


def test_a_failure_to_record_still_reverts_the_injection(fake_docker_cls, tmp_path, monkeypatch):
    """Bookkeeping must never outrank restoring the system.

    If the event file cannot be written after the failure is applied, the
    revert must still run - otherwise a disk problem leaves a bank service
    stopped indefinitely.
    """
    calls = {"n": 0}

    def flaky_write(path, events):
        calls["n"] += 1
        if calls["n"] == 1:      # the "start" record
            raise OSError("read-only file system")

    monkeypatch.setattr(session_module, "write_events", flaky_write)
    docker = fake_docker_cls()
    with pytest.raises((InjectionError, OSError)):
        run_injection(docker, "service_stop", "fineract", {}, hold_seconds=0,
                      events_path=tmp_path / "e.jsonl", sleep=lambda _s: None)
    assert "start" in docker.commands, "container was left stopped after a write failure"


@pytest.mark.parametrize("hold", [-1, -0.5])
def test_negative_hold_is_rejected_before_anything_is_touched(fake_docker_cls, tmp_path, hold):
    docker = fake_docker_cls()
    with pytest.raises(ValueError):
        run_injection(docker, "service_stop", "fineract", {}, hold_seconds=hold,
                      events_path=tmp_path / "e.jsonl", sleep=lambda _s: None)
    assert docker.commands == [], "the stack was touched before validation"


def test_unknown_component_touches_nothing(fake_docker_cls, tmp_path):
    docker = fake_docker_cls()
    with pytest.raises(ValueError):
        run_injection(docker, "service_stop", "prometheus", {}, hold_seconds=0,
                      events_path=tmp_path / "e.jsonl", sleep=lambda _s: None)
    assert docker.commands == []


def test_unknown_scenario_touches_nothing_after_inspection(fake_docker_cls, tmp_path):
    docker = fake_docker_cls()
    with pytest.raises(ValueError):
        run_injection(docker, "delete_everything", "fineract", {}, hold_seconds=0,
                      events_path=tmp_path / "e.jsonl", sleep=lambda _s: None)
    assert [c for c in docker.commands if c != "inspect"] == []


# --- CLI ---------------------------------------------------------------------------

def test_cli_list_shows_scenarios_and_targets(capsys):
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "service_stop" in out and "aort-fineract" in out


def test_cli_reports_an_invalid_parameter_as_exit_2(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli, "docker", lambda argv: (0, "running", ""))
    code = cli.main(["inject", "--scenario", "cpu_throttle", "--component", "fineract",
                     "--cpus", "nonsense", "--duration", "0",
                     "--events", str(tmp_path / "e.jsonl")])
    assert code == 2
    assert "cpus must be a number" in capsys.readouterr().err


def test_cli_reports_an_injection_failure_as_exit_3(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli, "docker", lambda argv: (1, "", "Cannot connect to the Docker daemon"))
    code = cli.main(["inject", "--scenario", "service_stop", "--component", "fineract",
                     "--duration", "0", "--events", str(tmp_path / "e.jsonl")])
    assert code == 3
    assert "Docker daemon" in capsys.readouterr().err


def test_cli_rejects_an_unknown_component_before_running(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["inject", "--scenario", "service_stop", "--component", "grafana"])
    assert exc.value.code == 2
