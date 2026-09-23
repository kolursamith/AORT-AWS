"""A real failure-and-recovery experiment, measured end to end.

Run with:  pytest --live

Pauses Fineract, watches the outage through its own health endpoint, releases
it, and measures how long recovery actually took.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest
import requests

from aort_recovery.experiment import run_recovery_experiment
from aort_recovery.ledger import DockerLedger
from aort_recovery.probes import http_probe

pytestmark = pytest.mark.live

HEALTH = "http://localhost:8080/fineract-provider/actuator/health"
# Absolute: pytest runs from the package directory, so a path relative to the
# repository root would silently fail and make the ledger look unreadable.
COMPOSE = str(Path(__file__).resolve().parents[3] / "banking" / "docker-compose.yml")


def docker(argv):
    proc = subprocess.run(list(argv), capture_output=True, text=True, timeout=300)
    return proc.returncode, proc.stdout, proc.stderr


def backup_epoch():
    """Newest successful backup, read from the sidecar - the RPO input."""
    code, out, _ = docker(["docker", "compose", "-f", COMPOSE, "exec", "-T",
                           "db-backup", "cat", "/textfile/aort_backup.prom"])
    if code != 0:
        return None
    for line in out.splitlines():
        if line.startswith("aort_backup_last_success_timestamp_seconds "):
            return float(line.split()[1])
    return None


@pytest.fixture(scope="module")
def outcome(tmp_path_factory):
    assert requests.get(HEALTH, timeout=15).json()["status"] == "UP", "stack not healthy"
    out_dir = tmp_path_factory.mktemp("recovery")
    return run_recovery_experiment(
        docker=docker,
        scenario="service_pause",
        component_id="fineract",
        probe=http_probe(HEALTH, timeout=3),
        ledger=DockerLedger(compose_file=COMPOSE),
        backup_at=backup_epoch(),
        failure_seconds=20,
        probe_interval=2,
        recovery_timeout=180,
        outcomes_path=out_dir / "outcomes.jsonl",
        events_path=out_dir / "events.jsonl",
    )


def test_the_experiment_recovered_the_service(outcome):
    assert outcome["recovered"] is True
    assert outcome["strategy"] == "unpause_service"


def test_measured_rto_is_real(outcome):
    rto = outcome["rto_seconds"]
    # It must exceed the injected outage and stay within the observation window.
    assert rto is not None and 20.0 <= rto <= 180.0, f"implausible RTO {rto}"


def test_availability_dropped_during_the_incident(outcome):
    assert 0.0 <= outcome["availability_ratio"] < 1.0
    assert outcome["probes_ok"] < outcome["probes_total"]


def test_rpo_exposure_was_measured_from_a_real_backup(outcome):
    assert outcome["rpo_seconds"] is not None, "no backup was available to bound RPO"
    assert outcome["rpo_seconds"] >= 0


def test_the_ledger_still_balances_after_recovery(outcome):
    assert outcome["transaction_integrity"] == "balanced"


def test_no_banking_rows_were_lost_by_a_pause(outcome):
    # A pause freezes the process; it must not destroy committed data.
    before, after = outcome["ledger_rows_before"], outcome["ledger_rows_after"]
    assert before and after
    for table, count in before.items():
        assert after.get(table, -1) >= count, f"{table} lost rows: {count} -> {after.get(table)}"


def test_outcome_satisfies_the_contract(outcome, validator):
    assert not list(validator.iter_errors(outcome))


def test_stack_is_healthy_afterwards():
    assert requests.get(HEALTH, timeout=15).json()["status"] == "UP"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        body = requests.get("http://localhost:9090/api/v1/query",
                            params={"query": 'up{job="fineract"}'}, timeout=10).json()
        result = body["data"]["result"]
        if result and float(result[0]["value"][1]) == 1.0:
            return
        time.sleep(5)
    pytest.fail("fineract did not return to up in Prometheus")
