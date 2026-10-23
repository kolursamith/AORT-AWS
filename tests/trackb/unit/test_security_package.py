"""Security validation package: manifest bookkeeping and metric helpers (no DB, no pandas)."""

from __future__ import annotations

import json

from backend.aorts.experiments import security_package as sp

ENV = {"git_commit": "abc123", "source_sha256": "f" * 64, "tracked_changes_uncommitted": False}


def _lines(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def test_manifest_records_start_and_end_with_output_hashes(tmp_path):
    man = sp.Manifest(tmp_path, "demo", ENV)

    def ok(run_id):
        return {"raw": sp.write_json(tmp_path / "raw" / f"{run_id}.json", {"x": 1})}, {"seed": 1}

    status, row = man.run("S", "cond", 1, {"k": "v"}, ok)
    start, end = _lines(tmp_path / "manifest.jsonl")
    assert status == "completed" and row == {"seed": 1}
    assert start["event"] == "start" and end["event"] == "end" and end["status"] == "completed"
    assert end["git_commit"] == "abc123" and end["config"] == {"k": "v"}
    assert len(end["outputs"]["raw"]["sha256"]) == 64


def test_manifest_keeps_failed_runs(tmp_path):
    man = sp.Manifest(tmp_path, "demo", ENV)

    def boom(run_id):
        raise RuntimeError("stack did not start")

    status, row = man.run("S", "cond", 2, {}, boom)
    end = _lines(tmp_path / "manifest.jsonl")[-1]
    assert status == "failed" and row is None
    assert end["status"] == "failed" and "stack did not start" in end["error"]


def test_jain_fairness_index():
    assert sp._jain([1.0, 1.0]) == 1.0
    assert sp._jain([1.0, 0.0]) == 0.5
    assert sp._jain([]) is None and sp._jain([0.0, 0.0]) is None


def test_paired_diff_needs_two_pairs_and_reports_ci():
    rows = [{"condition": "a", "seed": s, "m": 10 + s} for s in range(5)] + [
        {"condition": "b", "seed": s, "m": 5 + s} for s in range(5)
    ]
    d = sp.paired_diff(rows, "a", "b", "m")
    assert d["pairs"] == 5 and d["mean_diff"] == 5 and d["ci_excludes_zero"]
    assert sp.paired_diff(rows[:1] + rows[5:6], "a", "b", "m")["pairs"] == 1


def test_replay_expectations_cover_every_simulated_case():
    import inspect

    from backend.aorts.security.replay_simulator import ReplaySimulator

    src = inspect.getsource(ReplaySimulator)
    import re

    cases = set(re.findall(r'"(\d+[a-z]?_[a-z_]+)', src))
    assert cases, "no case names found"
    for case in cases:
        assert sp._expected(case), f"no HTTP expectation for replay case {case}"
    assert sp._expected("4_same_reversal_delivery_1") == {200}  # most specific prefix wins
    assert sp._expected("10b_repeated_nonce") == {401}


def test_verify_manifest_detects_tampered_and_missing_outputs(tmp_path):
    man = sp.Manifest(tmp_path, "demo", ENV)
    paths = {}

    def ok(key):
        paths[key] = sp.write_json(tmp_path / "raw" / f"{key}.json", {"v": 1})
        return {"raw": paths[key]}, {"seed": 1}

    man.run("S", "a", 1, {}, ok)
    man.run("S", "b", 2, {}, ok)
    clean = sp.verify_manifest(tmp_path)
    assert clean["outputs_verified"] == 2 and not clean["hash_mismatches"]
    first, second = list(paths.values())
    first.write_text('{"v": 2}', encoding="utf-8")  # tampered after the run
    second.unlink()
    result = sp.verify_manifest(tmp_path)
    assert len(result["hash_mismatches"]) == 1 and len(result["missing_files"]) == 1


def test_counter_lookup_matches_metrics_label_order():
    """Regression: labels are stored sorted, so a hand-built 'core_overloaded{kind=check' prefix missed them."""
    from backend.aorts.app.metrics import Metrics

    m = Metrics("switch")
    m.inc("core_overloaded", kind="check", bank="A3")
    m.inc("check_rejected", bank="A3", reason="RATE_LIMITED")
    c = m.snapshot()["counters"]
    assert sp._ctr(c, "core_overloaded", bank="A3", kind="check") == 1
    assert sp._ctr(c, "core_overloaded", kind="check", bank="A3") == 1
    assert sp._ctr(c, "check_rejected", reason="RATE_LIMITED", bank="A3") == 1
