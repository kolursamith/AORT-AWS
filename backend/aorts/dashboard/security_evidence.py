"""Load the security validation evidence package for the dashboard (pure functions, no Streamlit).

The package is written by ``backend.aorts.experiments.security_package`` under
``infra/trackb/evidence/security/``: one ``*-runs.csv`` per experiment (one row per completed run),
raw per-run files under ``raw/`` and the run manifest ``manifest.jsonl``. The dashboard shows the
newest experiment of each kind; it never computes results that are not in those files.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
EVIDENCE_DIR = Path(
    os.environ.get("AORTS_EVIDENCE_DIR", ROOT / "infra" / "trackb" / "evidence" / "security")
)
FLOOD_ORDER = [
    "normal",
    "flood_undefended",
    "flood_defended",
    "parallel_route_undefended",
    "parallel_route_defended",
]


def latest(kind: str, pattern: str = "*-runs.csv", base: Path | None = None) -> Path | None:
    """Newest file of an experiment kind (file names start with the UTC experiment timestamp)."""
    folder = (base or EVIDENCE_DIR) / kind
    files = sorted(folder.glob(pattern)) if folder.exists() else []
    return files[-1] if files else None


def runs(kind: str, base: Path | None = None) -> pd.DataFrame:
    path = latest(kind, base=base)
    return pd.read_csv(path) if path else pd.DataFrame()


def manifest(base: Path | None = None) -> pd.DataFrame:
    path = (base or EVIDENCE_DIR) / "manifest.jsonl"
    if not path.exists():
        return pd.DataFrame()
    ends = [
        r
        for r in (json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip())
        if r.get("event") == "end"
    ]
    cols = [
        "experiment",
        "scenario_id",
        "condition",
        "seed",
        "status",
        "started_at",
        "ended_at",
        "duration_s",
        "git_commit",
        "run_id",
        "error",
    ]
    return pd.DataFrame([{c: r.get(c) for c in cols} for r in ends])


def flood_by_condition(df: pd.DataFrame, metrics: list[str]) -> pd.DataFrame:
    """Mean of each metric per flooding condition, in the fixed condition order."""
    if df.empty:
        return df
    g = df.groupby("condition")[metrics].mean(numeric_only=True)
    return g.reindex([c for c in FLOOD_ORDER if c in g.index])


def per_bank_requests(df: pd.DataFrame, condition: str) -> pd.DataFrame:
    """Server-side received / admitted / rejected per bank (mean over seeds) for one condition."""
    rows = df[df["condition"] == condition]
    out = []
    for b in ("A1", "A2", "A3"):
        out.append(
            {
                "bank": b,
                "received": rows[f"{b}_server_received"].mean(),
                "admitted": rows[f"{b}_server_admitted"].mean(),
                "rejected": rows[f"{b}_server_rejected"].mean(),
            }
        )
    return pd.DataFrame(out)


def rejection_reasons(df: pd.DataFrame, condition: str) -> pd.DataFrame:
    rows = df[df["condition"] == condition]
    cols = [c for c in df.columns if c.startswith("A3_rejected_") and c != "A3_rejected_p50_ms"]
    return pd.DataFrame(
        [{"reason": c.removeprefix("A3_rejected_"), "requests": rows[c].mean()} for c in cols]
    )


def request_timeline(run_id: str, base: Path | None = None) -> pd.DataFrame:
    """Per-second admitted / rejected requests per bank for one flooding run (from its raw CSV)."""
    path = (base or EVIDENCE_DIR) / "flooding" / "raw" / f"{run_id}-requests.csv"
    if not path.exists():
        return pd.DataFrame()
    r = pd.read_csv(path)
    r["second"] = r["t"].astype(float).astype(int)
    r["outcome"] = r["status"].map(lambda s: "admitted" if s == 200 else "rejected")
    return r.groupby(["second", "bank", "outcome"]).size().rename("requests").reset_index()


def flood_run_ids(condition: str, base: Path | None = None) -> list[str]:
    m = manifest(base)
    if m.empty:
        return []
    m = m[
        (m["experiment"] == "flooding")
        & (m["condition"] == condition)
        & (m["status"] == "completed")
    ]
    return list(m["run_id"])


def poisoning_detection(df: pd.DataFrame) -> pd.DataFrame:
    """Detection rate (share of attack samples not TRUSTED) per case and arm."""
    if df.empty:
        return df
    return df.groupby(["case", "arm"])["detection_rate"].mean(numeric_only=True).unstack("arm")
