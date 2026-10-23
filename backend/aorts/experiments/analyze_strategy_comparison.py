"""Analyze strategy-comparison runs: per-strategy statistics and AORTS vs the STRONGEST baseline.

    python -m backend.aorts.experiments.analyze_strategy_comparison results/raw/strategy-*.jsonl

For every metric the strongest baseline is the baseline with the best mean on that metric (so AORTS
is compared with the hardest opponent for that metric, not a convenient one). Differences are paired
by seed and summarised with a percentile-bootstrap 95% CI (``paired_stats.py``). Failed runs are
counted and excluded; a seed is only paired if both strategies completed it.
"""

from __future__ import annotations

import csv
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

from backend.aorts.experiments.common import RESULTS
from backend.aorts.experiments.paired_stats import bootstrap_mean_ci, paired
from backend.aorts.experiments.provenance import file_sha256

# metric -> lower_is_better
METRICS = {
    "resolution_mean_s": True,
    "resolution_p95_s": True,
    "backlog_area": True,
    "backlog_peak": True,
    "status_check_amplification": True,
    "deadline_compliance": False,
    "escalated": True,
    "primary_goodput_per_s": False,
    "primary_p95_ms": True,
    "primary_success_ratio": False,
    "recovery_time_s": True,
    "invariant_violations": True,
}
AORTS_ONLY = [
    "capacity_rel_err_point",
    "capacity_rel_err_point_median",
    "capacity_low_signed_err_median",
    "capacity_low_overestimate_ratio",
]
SECURITY = [
    "flood_a3_sent",
    "flood_a3_admitted",
    "flood_containment_s",
    "financial_gate_rejections",
    "stale_jobs_dropped",
    "duplicate_jobs_injected",
    "recovery_blocked_by_reserve",
]


def load(paths: list[str]) -> tuple[list[dict], list[dict], list[dict]]:
    provs, rows, failed = [], [], []
    for p in paths:
        for line in Path(p).read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            if "provenance" in rec:
                provs.append({**rec["provenance"], "file": str(p), "sha256": file_sha256(Path(p))})
            elif "aborted" in rec:
                failed.append(rec)
            elif "error" in rec:
                failed.append(rec)
            else:
                rows.append(rec)
    return provs, rows, failed


def add_derived(rows: list[dict]) -> None:
    """Median capacity-estimation error per run, recomputed from the recorded samples (robust to the
    transition lag that dominates the mean right after a fault starts)."""
    for r in rows:
        s = [
            x
            for x in r.get("samples") or []
            if x.get("est_point_B") is not None
            and str(x.get("phase", "")).startswith(("degraded", "relapse"))
        ]
        if not s:
            continue
        errs = sorted(
            abs(x["est_point_B"] - x["truth_capacity_B_rps"]) / x["truth_capacity_B_rps"] for x in s
        )
        r["capacity_rel_err_point_median"] = round(statistics.median(errs), 4)
        low = sorted(
            (x["est_low_B"] - x["truth_capacity_B_rps"]) / x["truth_capacity_B_rps"]
            for x in s
            if x.get("est_low_B") is not None
        )
        if low:
            r["capacity_low_signed_err_median"] = round(statistics.median(low), 4)


def describe(values: list[float]) -> dict:
    v = [x for x in values if x is not None]
    if not v:
        return {"n": 0}
    ci = bootstrap_mean_ci(v) if len(v) > 1 else None
    return {
        "n": len(v),
        "mean": round(statistics.fmean(v), 4),
        "std": round(statistics.stdev(v), 4) if len(v) > 1 else None,
        "ci95": [round(ci[0], 4), round(ci[1], 4)] if ci else None,
    }


def analyze(rows: list[dict]) -> dict:
    by_scn = defaultdict(list)
    for r in rows:
        by_scn[r["scenario"]].append(r)
    out = {}
    for scn, rs in by_scn.items():
        strategies = sorted({r["strategy"] for r in rs})
        per = {s: {r["seed"]: r for r in rs if r["strategy"] == s} for s in strategies}
        stats = {
            s: {
                m: describe([r.get(m) for r in per[s].values()])
                for m in [*METRICS, *AORTS_ONLY, *SECURITY]
            }
            for s in strategies
        }
        baselines = [s for s in strategies if s != "aorts"]
        comparisons = {}
        if "aorts" in per and baselines:
            for m, lower in METRICS.items():
                means = {b: stats[b][m].get("mean") for b in baselines if stats[b][m].get("n")}
                if not means or not stats["aorts"][m].get("n"):
                    continue
                strongest = (min if lower else max)(means, key=means.get)
                a = {seed: r.get(m) for seed, r in per["aorts"].items()}
                b = {seed: r.get(m) for seed, r in per[strongest].items()}
                comparisons[m] = {
                    "strongest_baseline": strongest,
                    "lower_is_better": lower,
                    "aorts_mean": stats["aorts"][m]["mean"],
                    "baseline_mean": means[strongest],
                    **paired(a, b, lower_is_better=lower),
                }
        seeds = sorted({r["seed"] for r in rs})
        out[scn] = {
            "strategies": strategies,
            "seeds": len(seeds),
            "runs": len(rs),
            "stats": stats,
            "aorts_vs_strongest_baseline": comparisons,
            "held_out": any(r.get("held_out") for r in rs),
        }
    return out


def to_markdown(result: dict, failed: list[dict]) -> str:
    lines = []
    for scn, r in result.items():
        lines.append(
            f"### {scn}{' (held-out)' if r['held_out'] else ''}: {r['seeds']} seeds, {r['runs']} completed runs\n"
        )
        hdr = ["metric"] + r["strategies"]
        lines.append("| " + " | ".join(hdr) + " |")
        lines.append("|" + "---|" * len(hdr))
        for m in [*METRICS, *AORTS_ONLY]:
            cells = []
            for s in r["strategies"]:
                d = r["stats"][s][m]
                if not d.get("n"):
                    cells.append("-")
                else:
                    ci = d.get("ci95")
                    cells.append(
                        f"{d['mean']:.3g}" + (f" [{ci[0]:.3g}, {ci[1]:.3g}]" if ci else "")
                    )
            lines.append(f"| {m} | " + " | ".join(cells) + " |")
        lines.append(
            "\n**AORTS vs strongest baseline (paired by seed, bootstrap 95% CI of mean difference AORTS - baseline):**\n"
        )
        lines.append(
            "| metric | strongest baseline | AORTS mean | baseline mean | mean diff [95% CI] | pairs | verdict |"
        )
        lines.append("|---|---|---|---|---|---|---|")
        for m, c in r["aorts_vs_strongest_baseline"].items():
            diff = (
                f"{c['mean_diff']:.3g} [{c['ci95'][0]:.3g}, {c['ci95'][1]:.3g}]"
                if "ci95" in c
                else "-"
            )
            lines.append(
                f"| {m} | {c['strongest_baseline']} | {c['aorts_mean']:.3g} | {c['baseline_mean']:.3g} | "
                f"{diff} | {c['pairs']} | {c['verdict']} |"
            )
        lines.append("")
    if failed:
        lines.append(
            f"Failed/aborted runs excluded: {len(failed)} ({[f.get('error') or f.get('aborted') for f in failed][:5]})"
        )
    return "\n".join(lines)


def main() -> None:
    paths = sys.argv[1:] or sorted(str(p) for p in (RESULTS / "raw").glob("strategy-*.jsonl"))
    provs, rows, failed = load(paths)
    add_derived(rows)
    result = analyze(rows)
    (RESULTS / "processed").mkdir(parents=True, exist_ok=True)
    stem = "strategy-summary"
    summary = {"inputs": provs, "failed_runs": len(failed), "analysis": result}
    (RESULTS / "processed" / f"{stem}.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8"
    )
    md = to_markdown(result, failed)
    (RESULTS / "processed" / f"{stem}.md").write_text(md, encoding="utf-8")
    flat = [
        {k: v for k, v in r.items() if k not in ("samples", "phase_marks", "predispatch_gate")}
        for r in rows
    ]
    if flat:
        keys = sorted({k for r in flat for k in r})
        with (RESULTS / "processed" / f"{stem}-runs.csv").open(
            "w", newline="", encoding="utf-8"
        ) as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(flat)
    print(md)


if __name__ == "__main__":
    main()
