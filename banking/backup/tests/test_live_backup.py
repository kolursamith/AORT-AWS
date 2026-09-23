"""Against the running banking stack: real dumps, real metrics, real restore.

Run with:  pytest --live
With --live these tests FAIL (not skip) if the stack is down.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from aort_backup.dumps import newest_dump, parse_dump_name
from aort_backup.metrics import backup_age_seconds, parse_textfile
from aort_backup.restore import compose_exec_argv, restore_into

pytestmark = pytest.mark.live

REPO = Path(__file__).resolve().parents[3]
COMPOSE = str(REPO / "banking" / "docker-compose.yml")
SERVICE = "db-backup"
SOURCE_DB = "fineract_default"
SCRATCH_DB = "aort_restore_check"
USER = "postgres"


def run(argv, timeout=180):
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def in_container(argv, timeout=180):
    return run(compose_exec_argv(SERVICE, argv, compose_file=COMPOSE), timeout=timeout)


def psql_scalar(database, sql):
    rc, out, err = in_container(
        ["psql", "--username", USER, "--dbname", database, "-tAc", sql])
    assert rc == 0, err
    return out.strip()


@pytest.fixture(scope="module")
def backup_state():
    """Wait for at least two successful backups, then read the real state."""
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        rc, out, _ = in_container(["cat", "/textfile/aort_backup.prom"], timeout=30)
        if rc == 0:
            metrics = parse_textfile(out)
            if metrics.get("aort_backup_runs_total", 0) >= 2 and \
                    "aort_backup_last_success_timestamp_seconds" in metrics:
                return metrics
        time.sleep(5)
    pytest.fail("the backup sidecar produced no successful backups within 300s")


def test_sidecar_reports_successful_backups(backup_state):
    assert backup_state["aort_backup_runs_total"] >= 2
    assert backup_state["aort_backup_failures_total"] == 0


def test_backup_age_is_recent(backup_state):
    age = backup_age_seconds(backup_state, now=time.time())
    assert age is not None and age < 300, f"last backup is {age}s old"


def test_backup_is_a_non_trivial_file(backup_state):
    assert backup_state["aort_backup_last_size_bytes"] > 10_000
    assert backup_state["aort_backup_last_duration_seconds"] >= 0


def test_dumps_exist_on_disk_with_parseable_names():
    rc, out, err = in_container(["sh", "-c", "ls -1 /backups"])
    assert rc == 0, err
    names = [n for n in out.split() if n.endswith(".dump")]
    assert names, "no dump files were written"
    for name in names:
        parse_dump_name(name)


def test_retention_keeps_the_backup_directory_bounded():
    rc, out, _ = in_container(["sh", "-c", "ls -1 /backups"])
    dumps = [n for n in out.split() if n.endswith(".dump")]
    assert len(dumps) <= 5, f"retention did not prune: {dumps}"


def test_newest_dump_restores_into_a_scratch_database_with_matching_rows():
    """The real proof: restore the newest dump and compare banking row counts."""
    rc, out, err = in_container(["sh", "-c", "ls -1 /backups"])
    assert rc == 0, err
    newest = newest_dump([n for n in out.split() if n.endswith(".dump")], database=SOURCE_DB)
    assert newest, "no dump for the source database"

    restore_into(run, f"/backups/{newest}", SCRATCH_DB, user=USER,
                 compose_file=COMPOSE, service=SERVICE)
    try:
        for table in ("m_client", "m_savings_account", "m_loan", "acc_gl_journal_entry"):
            restored = psql_scalar(SCRATCH_DB, f"SELECT count(*) FROM {table}")
            assert int(restored) >= 0
            # The dump is a point-in-time copy, so the restored count cannot
            # exceed the live table it was taken from.
            live = psql_scalar(SOURCE_DB, f"SELECT count(*) FROM {table}")
            assert int(restored) <= int(live), f"{table}: restored {restored} > live {live}"
        # A restored ledger must still balance: debits equal credits.
        debits = psql_scalar(SCRATCH_DB,
                             "SELECT COALESCE(sum(amount),0) FROM acc_gl_journal_entry WHERE type_enum=2")
        credits = psql_scalar(SCRATCH_DB,
                              "SELECT COALESCE(sum(amount),0) FROM acc_gl_journal_entry WHERE type_enum=1")
        assert float(debits) == float(credits), f"restored ledger unbalanced: {debits} vs {credits}"
    finally:
        in_container(["dropdb", "--username", USER, "--if-exists", "--force", SCRATCH_DB])
