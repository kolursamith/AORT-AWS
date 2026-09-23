"""Real injections into the running stack, and the telemetry reaction.

Run with:  pytest --live

These tests stop and pause real containers. Every one restores what it touched,
and the last test asserts the whole stack is healthy again afterwards.
"""

from __future__ import annotations

import json
import subprocess
import time

import pytest
import requests

from aort_injection.session import run_injection

pytestmark = pytest.mark.live

PROM = "http://localhost:9090"


def docker(argv):
    proc = subprocess.run(list(argv), capture_output=True, text=True, timeout=180)
    return proc.returncode, proc.stdout, proc.stderr


def container_state(name):
    return docker(["docker", "inspect", "--format", "{{.State.Status}}", name])[1].strip()


def promql(query):
    body = requests.get(f"{PROM}/api/v1/query", params={"query": query}, timeout=10).json()
    result = body["data"]["result"]
    return float(result[0]["value"][1]) if result else None


def wait_for(predicate, timeout=90, interval=5):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    return last


def test_backup_stop_is_applied_reverted_and_recorded(tmp_path):
    """Stopping backups degrades RPO while the bank keeps serving traffic."""
    events_path = tmp_path / "events.jsonl"
    assert container_state("aort-db-backup") == "running"

    run_injection(docker, "backup_stop", "backup", {}, hold_seconds=15,
                  events_path=events_path)

    assert container_state("aort-db-backup") == "running", "sidecar was not restored"
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    assert [e["phase"] for e in events] == ["start", "end"]
    assert len({e["injection_id"] for e in events}) == 1
    assert events[0]["container"] == "aort-db-backup"


def test_events_validate_against_the_contract(tmp_path, validator):
    events_path = tmp_path / "events.jsonl"
    run_injection(docker, "backup_stop", "backup", {}, hold_seconds=5,
                  events_path=events_path)
    for line in events_path.read_text(encoding="utf-8").splitlines():
        assert not list(validator.iter_errors(json.loads(line)))


def test_pausing_fineract_is_visible_in_telemetry_then_recovers(tmp_path):
    """The research chain's first link: an injected failure must be observable."""
    assert promql('up{job="fineract"}') == 1.0, "fineract was already down"

    events_path = tmp_path / "events.jsonl"
    # Long enough for Prometheus (10s scrape) to register the outage.
    run_injection(docker, "service_pause", "fineract", {}, hold_seconds=30,
                  events_path=events_path)

    # During the hold the scrape must have failed at least once.
    dropped = promql('min_over_time(up{job="fineract"}[3m])')
    assert dropped == 0.0, f"telemetry did not observe the outage (min up = {dropped})"

    assert container_state("aort-fineract") == "running"
    recovered = wait_for(lambda: promql('up{job="fineract"}') == 1.0, timeout=120)
    assert recovered, "fineract did not return to up after the injection"


def test_stack_is_healthy_after_all_injections():
    for name in ("aort-fineract", "aort-postgres", "aort-db-backup"):
        assert container_state(name) == "running", f"{name} left unhealthy"
    assert promql("pg_up") == 1.0
    health = requests.get("http://localhost:8080/fineract-provider/actuator/health", timeout=15)
    assert health.json().get("status") == "UP"
