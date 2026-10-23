"""Stage 10 campaign manifest: per-run rows and resumption of an interrupted campaign."""

from __future__ import annotations

import json

from backend.aorts.experiments.stage10_security import Campaign

ENV = {"git_commit": "abc", "tracked_changes_uncommitted": False, "source_sha256": "d1"}


def _fn(calls):
    def fn(key):
        calls.append(key)
        return {}, {"condition": "x", "metric": len(calls)}

    return fn


def test_rows_are_persisted_and_completed_runs_are_resumed(tmp_path):
    out, raw, calls = tmp_path / "out", tmp_path / "raw", []
    first = Campaign(out, "s10-test", ENV, raw, None)
    assert first.run("S", "x", 1, {"a": 1}, _fn(calls)) == (
        "completed",
        {"condition": "x", "metric": 1},
    )
    end = [json.loads(line) for line in first.path.read_text().splitlines()][-1]
    assert "row" in end["outputs"]

    again = Campaign(out, "s10-test", ENV, raw, first.experiment_id)
    status, row = again.run("S", "x", 1, {"a": 1}, _fn(calls))
    assert (status, row["metric"], len(calls)) == ("completed", 1, 1)  # not re-run
    again.run("S", "x", 1, {"a": 2}, _fn(calls))  # other configuration: runs
    again.run("S", "x", 2, {"a": 1}, _fn(calls))  # other seed: runs
    assert len(calls) == 3


def test_resume_ignores_other_digests_failures_and_tampered_rows(tmp_path):
    out, raw, calls = tmp_path / "out", tmp_path / "raw", []
    first = Campaign(out, "s10-test", ENV, raw, None)
    first.run("S", "x", 1, {}, _fn(calls))

    def boom(key):
        raise RuntimeError("interrupted")

    assert first.run("S", "x", 2, {}, boom)[0] == "failed"

    other = Campaign(out, "s10-test", {**ENV, "source_sha256": "d2"}, raw, first.experiment_id)
    assert other.done == {}  # another source digest is never mixed in

    same = Campaign(out, "s10-test", ENV, raw, first.experiment_id)
    assert [k[1] for k in same.done] == [1]  # the failed seed 2 is not reused
    row_file = next(raw.glob("*-row.json"))
    row_file.write_text('{"metric": 999}')
    assert Campaign(out, "s10-test", ENV, raw, first.experiment_id).done == {}
