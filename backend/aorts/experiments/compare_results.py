"""Aggregate the newest processed CSVs into mean / std / 95% CI tables (results/processed/summary-*.csv).

Only reads files produced by the experiment scripts; never invents numbers.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

from backend.aorts.experiments.common import RESULTS, stats, write_outputs


def newest(prefix: str) -> Path | None:
    files = sorted((RESULTS / "processed").glob(f"{prefix}-2*.csv"))
    files = [f for f in files if "-ticks" not in f.name and not f.name.startswith("summary")]
    return files[-1] if files else None


def load(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def aggregate(rows, keys, metrics):
    groups = defaultdict(list)
    for r in rows:
        groups[tuple(r[k] for k in keys)].append(r)
    out = []
    for gk, rs in sorted(groups.items()):
        row = dict(zip(keys, gk, strict=True))
        for m in metrics:
            s = stats([num(r.get(m)) for r in rs])
            row[f"{m}_n"], row[f"{m}_mean"], row[f"{m}_std"] = s["n"], s["mean"], s["std"]
            row[f"{m}_ci95"] = s["ci95"]
        out.append(row)
    return out


def main() -> None:
    produced = {}
    flood_metrics = [
        "a3_admitted",
        "a1_admitted_ratio",
        "a2_admitted_ratio",
        "legit_success_ratio",
        "legit_check_p95_during_ms",
        "legit_check_p95_after_ms",
        "detection_s",
        "containment_s",
        "payment_p95_ms",
    ]
    for prefix in ("flooding", "flooding-constrained"):
        if f := newest(prefix):
            produced[prefix] = aggregate(
                load(f), ["scenario", "attack_rps", "defenses"], flood_metrics
            )
    if f := newest("poisoning"):
        produced["poisoning"] = aggregate(
            load(f),
            ["case", "arm"],
            [
                "mean_budget_during_attack",
                "max_budget_during_attack",
                "mean_actual_delay_ms",
                "ticks_over_1000ms",
                "detection_tick_offset",
            ],
        )
    if f := newest("replay"):
        rows = load(f)
        produced["replay"] = aggregate(
            rows, ["case", "auto_reverse"], ["http_status", "reversal_postings", "ledger_postings"]
        )
    for name, rows in produced.items():
        print(name, write_outputs(f"summary-{name}", {"rows": rows}, rows))
    if not produced:
        print("No experiment CSVs found in results/processed - run the experiment scripts first.")


if __name__ == "__main__":
    main()
