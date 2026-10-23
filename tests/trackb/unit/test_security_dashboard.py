"""Dashboard loaders for the security validation evidence package (needs the dashboard extras)."""

from __future__ import annotations

import pytest

pd = pytest.importorskip(
    "pandas", reason="dashboard extras (pandas) not installed in this environment"
)

from backend.aorts.dashboard import security_evidence as ev  # noqa: E402
from backend.aorts.experiments import security_package as sp  # noqa: E402

ENV = {"git_commit": "abc123", "source_sha256": "f" * 64, "tracked_changes_uncommitted": False}


def _write_runs(base, kind, rows, name="secpkg-x-20261022T000000Z-runs.csv"):
    folder = base / kind
    folder.mkdir(parents=True)
    pd.DataFrame(rows).to_csv(folder / name, index=False)


def test_dashboard_loaders_shape_flooding_results(tmp_path):
    rows = []
    for cond in ("flood_defended", "normal"):
        for seed in (1, 2):
            rows.append(
                {
                    "condition": cond,
                    "seed": seed,
                    **{
                        f"{b}_server_{k}": 10 * seed
                        for b in ("A1", "A2", "A3")
                        for k in ("received", "admitted", "rejected")
                    },
                    "A3_rejected_RATE_LIMITED": 4,
                    "A3_rejected_p50_ms": 1.0,
                    "legit_success_during": 1.0,
                }
            )
    _write_runs(tmp_path, "flooding", rows)
    df = ev.runs("flooding", base=tmp_path)
    assert list(ev.flood_by_condition(df, ["legit_success_during"]).index) == [
        "normal",
        "flood_defended",
    ]
    banks = ev.per_bank_requests(df, "flood_defended")
    assert list(banks["bank"]) == ["A1", "A2", "A3"] and banks["received"].iloc[0] == 15
    reasons = ev.rejection_reasons(df, "flood_defended")
    assert list(reasons["reason"]) == ["RATE_LIMITED"]  # the latency column is not a reason


def test_dashboard_manifest_and_missing_package(tmp_path):
    assert ev.manifest(tmp_path).empty and ev.runs("flooding", base=tmp_path).empty
    man = sp.Manifest(tmp_path, "flooding", ENV)
    man.run("S", "flood_defended", 1, {}, lambda rid: ({}, {"seed": 1}))
    m = ev.manifest(tmp_path)
    assert len(m) == 1 and m["status"].iloc[0] == "completed"
    assert ev.flood_run_ids("flood_defended", base=tmp_path) == [m["file_key"].iloc[0]]
