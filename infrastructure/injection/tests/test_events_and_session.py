"""Injection events are the ground truth, so they must be exact."""

from __future__ import annotations

import json
import uuid

import pytest

from aort_injection import session as session_module
from aort_injection.events import build_event, format_timestamp, write_events
from aort_injection.session import InjectionError, run_injection

AT = 1_000_000_000.5
STAMP = "2001-09-09T01:46:40.500Z"


def test_timestamps_are_utc_with_millisecond_precision():
    assert format_timestamp(AT) == STAMP


def test_event_satisfies_the_contract(validator):
    event = build_event("service_stop", "fineract", "aort-fineract", {}, "start",
                        at=AT, injection_id=str(uuid.uuid4()))
    assert not list(validator.iter_errors(event))
    assert event["at"] == STAMP and event["error"] is None


def test_failed_event_records_why(validator):
    event = build_event("service_stop", "fineract", "aort-fineract", {}, "failed",
                        at=AT, injection_id=str(uuid.uuid4()), error="no such container")
    assert not list(validator.iter_errors(event))
    assert event["error"] == "no such container"


def test_parameters_are_stringified_for_the_contract(validator):
    event = build_event("cpu_throttle", "fineract", "aort-fineract", {"cpus": 0.2},
                        "start", at=AT, injection_id=str(uuid.uuid4()))
    assert event["parameters"] == {"cpus": "0.2"}
    assert not list(validator.iter_errors(event))


def test_events_are_appended_as_jsonl(tmp_path, validator):
    path = tmp_path / "events.jsonl"
    identifier = str(uuid.uuid4())
    for phase in ("start", "end"):
        write_events(path, [build_event("service_pause", "postgres", "aort-postgres", {},
                                        phase, at=AT, injection_id=identifier)])
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for line in lines:
        assert not list(validator.iter_errors(json.loads(line)))


# --- session: inject, hold, always revert ----------------------------------------

def test_injection_applies_then_reverts(fake_docker_cls, tmp_path):
    docker = fake_docker_cls()
    run_injection(docker, "service_stop", "fineract", {}, hold_seconds=0,
                  events_path=tmp_path / "e.jsonl", sleep=lambda _s: None)
    assert docker.commands == ["inspect", "stop", "start", "inspect"]


def test_injection_records_start_and_end_sharing_one_id(fake_docker_cls, tmp_path, validator):
    path = tmp_path / "e.jsonl"
    run_injection(fake_docker_cls(), "service_pause", "postgres", {}, hold_seconds=0,
                  events_path=path, sleep=lambda _s: None)
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [e["phase"] for e in events] == ["start", "end"]
    assert len({e["injection_id"] for e in events}) == 1
    assert uuid.UUID(events[0]["injection_id"]).version == 4
    for event in events:
        assert not list(validator.iter_errors(event))


def test_end_event_is_never_before_the_start_event(fake_docker_cls, tmp_path):
    path = tmp_path / "e.jsonl"
    run_injection(fake_docker_cls(), "service_stop", "fineract", {}, hold_seconds=0,
                  events_path=path, sleep=lambda _s: None)
    start, end = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert end["at"] >= start["at"]


def test_hold_duration_is_respected(fake_docker_cls, tmp_path):
    slept = []
    run_injection(fake_docker_cls(), "service_stop", "fineract", {}, hold_seconds=45,
                  events_path=tmp_path / "e.jsonl", sleep=slept.append)
    assert slept == [45]


def test_failure_to_apply_records_a_failed_event_and_does_not_revert(
        fake_docker_cls, tmp_path, validator):
    path = tmp_path / "e.jsonl"
    docker = fake_docker_cls(results=[(0, "running", ""), (1, "", "No such container")])
    with pytest.raises(InjectionError, match="No such container"):
        run_injection(docker, "service_stop", "fineract", {}, hold_seconds=0,
                      events_path=path, sleep=lambda _s: None)
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [e["phase"] for e in events] == ["failed"]
    assert not list(validator.iter_errors(events[0]))
    assert "start" not in docker.commands[1:]  # nothing to revert


def test_revert_is_attempted_even_if_the_hold_is_interrupted(fake_docker_cls, tmp_path):
    def boom(_seconds):
        raise KeyboardInterrupt

    docker = fake_docker_cls()
    with pytest.raises(KeyboardInterrupt):
        run_injection(docker, "service_stop", "fineract", {}, hold_seconds=30,
                      events_path=tmp_path / "e.jsonl", sleep=boom)
    # The stack must not be left broken because the operator pressed Ctrl-C.
    assert "start" in docker.commands


def test_a_failed_revert_is_loud(fake_docker_cls, tmp_path):
    docker = fake_docker_cls(results=[(0, "running", ""), (0, "", ""), (1, "", "daemon gone")])
    with pytest.raises(InjectionError, match="daemon gone"):
        run_injection(docker, "service_stop", "fineract", {}, hold_seconds=0,
                      events_path=tmp_path / "e.jsonl", sleep=lambda _s: None)


def test_injection_refuses_a_target_that_is_not_running(fake_docker_cls, tmp_path):
    docker = fake_docker_cls(state={"aort-fineract": "exited"})
    with pytest.raises(InjectionError, match="not running"):
        run_injection(docker, "service_stop", "fineract", {}, hold_seconds=0,
                      events_path=tmp_path / "e.jsonl", sleep=lambda _s: None)


def test_default_sleep_is_real_time(monkeypatch, fake_docker_cls, tmp_path):
    calls = []
    monkeypatch.setattr(session_module.time, "sleep", calls.append)
    run_injection(fake_docker_cls(), "service_stop", "fineract", {}, hold_seconds=2,
                  events_path=tmp_path / "e.jsonl")
    assert calls == [2]
