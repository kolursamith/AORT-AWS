"""Restore command construction and orchestration.

A backup nobody has restored is not evidence of anything, so restore is built
and tested alongside the backup itself.
"""

from __future__ import annotations

import pytest

from aort_backup.restore import (
    RestoreError,
    build_createdb_argv,
    build_dropdb_argv,
    build_pg_restore_argv,
    compose_exec_argv,
    restore_into,
)

DUMP = "/backups/fineract_default-20260923T101500Z.dump"


def test_pg_restore_argv_is_explicit_and_ownership_free():
    assert build_pg_restore_argv(DUMP, "scratch_db", user="postgres") == [
        "pg_restore", "--username", "postgres", "--dbname", "scratch_db",
        "--no-owner", "--no-privileges", "--exit-on-error", DUMP,
    ]


def test_createdb_and_dropdb_argv():
    assert build_createdb_argv("scratch_db", user="postgres") == [
        "createdb", "--username", "postgres", "scratch_db"]
    assert build_dropdb_argv("scratch_db", user="postgres") == [
        "dropdb", "--username", "postgres", "--if-exists", "--force", "scratch_db"]


def test_dropdb_forces_out_existing_sessions():
    """Monitoring holds connections to every database it discovers.

    postgres_exporter runs with auto-discovery, so it connects to a restore
    target as soon as it exists. Without --force the drop fails with "database
    is being accessed by other users" and recovery is blocked by observation.
    """
    assert "--force" in build_dropdb_argv("scratch_db", user="postgres")


def test_compose_exec_wraps_without_a_tty():
    argv = compose_exec_argv("db-backup", ["psql", "-c", "select 1"], compose_file="banking/docker-compose.yml")
    assert argv == ["docker", "compose", "-f", "banking/docker-compose.yml",
                    "exec", "-T", "db-backup", "psql", "-c", "select 1"]


@pytest.mark.parametrize("bad", ["", "scratch db", "drop;table", "a" * 64, "1abc", "-x"])
def test_invalid_target_database_names_are_rejected(bad):
    # The name reaches a shell-executed command, so it is validated, not escaped.
    with pytest.raises(ValueError):
        build_createdb_argv(bad, user="postgres")


def test_restore_into_drops_creates_then_restores(fake_runner_cls):
    runner = fake_runner_cls()
    restore_into(runner, DUMP, "scratch_db", user="postgres",
                 compose_file="banking/docker-compose.yml", service="db-backup")
    executed = [call[call.index("db-backup") + 1] for call in runner.calls]
    assert executed == ["dropdb", "createdb", "pg_restore"]


def test_restore_into_reports_the_failing_step(fake_runner_cls):
    runner = fake_runner_cls(results=[(0, "", ""), (0, "", ""), (1, "", "could not read dump")])
    with pytest.raises(RestoreError, match="could not read dump"):
        restore_into(runner, DUMP, "scratch_db", user="postgres",
                     compose_file="banking/docker-compose.yml", service="db-backup")


def test_restore_into_stops_before_restoring_if_create_fails(fake_runner_cls):
    runner = fake_runner_cls(results=[(0, "", ""), (1, "", "permission denied")])
    with pytest.raises(RestoreError, match="permission denied"):
        restore_into(runner, DUMP, "scratch_db", user="postgres",
                     compose_file="banking/docker-compose.yml", service="db-backup")
    assert len(runner.calls) == 2  # never reached pg_restore
