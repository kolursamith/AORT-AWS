"""Bug-hunt pass: corrupted state, hostile input and the CLI's failure paths."""

from __future__ import annotations

import pytest

from aort_backup import __main__ as cli
from aort_backup.metrics import backup_age_seconds
from aort_backup.restore import compose_exec_argv

GOOD_STATE = """\
aort_backup_runs_total 4
aort_backup_failures_total 0
aort_backup_last_success_timestamp_seconds 1790178433
aort_backup_last_duration_seconds 0.39
aort_backup_last_size_bytes 1053300
"""
NO_BACKUP_YET = "aort_backup_runs_total 1\naort_backup_failures_total 1\n"


# --- age must never be a meaningless number ------------------------------------

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_timestamp_gives_unknown_age_not_a_number(bad):
    # A corrupted metrics file must read as "unknown", never as an age the
    # optimizer could treat as a real RPO.
    assert backup_age_seconds({"aort_backup_last_success_timestamp_seconds": bad}, now=1000.0) is None


def test_non_finite_now_gives_unknown_age():
    metrics = {"aort_backup_last_success_timestamp_seconds": 1000.0}
    assert backup_age_seconds(metrics, now=float("nan")) is None


# --- CLI --------------------------------------------------------------------------

def fake_container(monkeypatch, stdout, code=0, stderr=""):
    calls = []

    def fake(argv, compose_file):
        calls.append(list(argv))
        return code, stdout, stderr

    monkeypatch.setattr(cli, "in_container", fake)
    return calls


def test_status_reports_age_and_succeeds(monkeypatch, capsys):
    fake_container(monkeypatch, GOOD_STATE)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "age (RPO)" in out and "runs         : 4" in out


def test_status_without_any_successful_backup_exits_nonzero(monkeypatch, capsys):
    fake_container(monkeypatch, NO_BACKUP_YET)
    assert cli.main(["status"]) == 1
    out = capsys.readouterr().out
    assert "no successful backup yet" in out
    # Absent values must not be printed as "nan".
    assert "nan" not in out.lower()


def test_status_reports_unreadable_state_as_an_error(monkeypatch, capsys):
    fake_container(monkeypatch, "", code=1, stderr="service not running")
    assert cli.main(["status"]) == 2
    assert "service not running" in capsys.readouterr().err


def test_list_prints_only_dump_files(monkeypatch, capsys):
    fake_container(monkeypatch, "fineract_default-20260923T101500Z.dump\nnotes.txt\n")
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "fineract_default-20260923T101500Z.dump" in out
    assert "notes.txt" not in out
    assert "1 dump(s)" in out


def test_restore_without_a_matching_dump_exits_nonzero(monkeypatch, capsys):
    fake_container(monkeypatch, "fineract_default-20260923T101500Z.dump\n")
    assert cli.main(["restore", "--into", "scratch_db", "--database", "other_db"]) == 1
    assert "no dump found" in capsys.readouterr().err


def test_restore_rejects_an_unsafe_target_name(monkeypatch, capsys):
    fake_container(monkeypatch, "fineract_default-20260923T101500Z.dump\n")
    assert cli.main(["restore", "--into", "drop;table"]) == 3
    assert "unsafe or invalid" in capsys.readouterr().err


# --- command construction ----------------------------------------------------------

def test_compose_exec_requires_a_command():
    with pytest.raises(ValueError):
        compose_exec_argv("db-backup", [], compose_file="banking/docker-compose.yml")
