"""Stage 12 analysis: paired statistics, tables and SVG figures from the recorded runs only.

Nothing is simulated here. Every number comes from ``results/processed/stage12/runs.jsonl``
rows produced from one source digest and one frozen design digest (rows from any other digest
are counted and excluded, never mixed). Paired differences use the identical workload of each
seed; a 95 % percentile-bootstrap interval of the mean difference (10 000 resamples, fixed
seed) is given only when there are at least 10 pairs and the differences are not all equal.
No significance test or p-value is claimed.
"""

from __future__ import annotations

import csv
import json
import statistics
from collections import Counter
from html import escape
from pathlib import Path

from intelligence.experiments import stage12_evaluation as ev

HEADLINE = (
    "new_ambiguous",
    "resolved_by_check",
    "unresolved_at_end",
    "checks_released",
    "payment_latency_p95_s",
    "payment_latency_p99_s",
    "deadline_completion",
    "recovery_goodput",
    "time_to_recovery_s",
    "fairness_jain",
    "safety_violations",
)


def _fmt(x, nd=2) -> str:
    if x is None:
        return "-"
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


def _ci(p: dict | None) -> str:
    if not p:
        return "-"
    ci = p.get("ci95_bootstrap")
    base = f"{p['mean']:+.1f} (med {p['median']:+.1f})"
    return base + (f" [{ci[0]:+.1f}, {ci[1]:+.1f}]" if ci else "")


def aggregate(rows: dict, scenario: str, arm: str) -> dict:
    out = {}
    for metric in HEADLINE:
        get = ev.METRICS[metric][0]
        vals = [
            get(rows[(scenario, arm, s)])
            for s in ev.seeds_for(scenario)
            if (scenario, arm, s) in rows
        ]
        vals = [float(v) for v in vals if v is not None]
        out[metric] = {
            "n": len(vals),
            "mean": statistics.fmean(vals) if vals else None,
            "median": statistics.median(vals) if vals else None,
            "sd": statistics.stdev(vals) if len(vals) > 1 else None,
        }
    return out


def _svg_diffs(title: str, subtitle: str, items: list[tuple[str, dict | None]], unit: str) -> str:
    """Horizontal dot plot: mean paired difference per arm with its bootstrap interval."""

    items = [(k, p) for k, p in items if p]
    width, row_h, left, right, top = 900, 26, 210, 110, 70  # right: room for value labels
    height = top + row_h * len(items) + 50
    lo = min([0.0] + [min(p["mean"], *(p.get("ci95_bootstrap") or (p["mean"],))) for _, p in items])
    hi = max([0.0] + [max(p["mean"], *(p.get("ci95_bootstrap") or (p["mean"],))) for _, p in items])
    span = (hi - lo) or 1.0
    lo, hi = lo - 0.05 * span, hi + 0.05 * span

    def x(v):
        return left + (v - lo) / (hi - lo) * (width - left - right)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="system-ui, sans-serif">',
        "<style>.bg{fill:#fcfcfb}.t{fill:#0b0b0b}.m{fill:#52514e}.g{stroke:#e4e3df}"
        ".z{stroke:#52514e}.d{fill:#2a78d6}.w{stroke:#2a78d6}"
        "@media (prefers-color-scheme: dark){.bg{fill:#1a1a19}.t{fill:#f0efec}"
        ".m{fill:#c3c2b7}.g{stroke:#3a3936}.z{stroke:#c3c2b7}.d{fill:#3987e5}"
        ".w{stroke:#3987e5}}</style>",
        f'<rect class="bg" width="{width}" height="{height}"/>',
        f'<text class="t" x="16" y="24" font-size="15" font-weight="600">{escape(title)}</text>',
        f'<text class="m" x="16" y="44" font-size="12">{escape(subtitle)}</text>',
        f'<line class="z" x1="{x(0):.1f}" y1="{top - 8}" x2="{x(0):.1f}" '
        f'y2="{top + row_h * len(items)}" stroke-dasharray="3 3"/>',
    ]
    for i, (name, p) in enumerate(items):
        y = top + row_h * i + row_h / 2
        parts.append(
            f'<line class="g" x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}"/>'
        )
        parts.append(
            f'<text class="t" x="{left - 8}" y="{y + 4:.1f}" font-size="12" '
            f'text-anchor="end">{escape(name)} (n={p["n"]})</text>'
        )
        if p.get("ci95_bootstrap"):
            a, b = p["ci95_bootstrap"]
            parts.append(
                f'<line class="w" x1="{x(a):.1f}" y1="{y:.1f}" x2="{x(b):.1f}" y2="{y:.1f}" '
                'stroke-width="2"/>'
            )
        parts.append(f'<circle class="d" cx="{x(p["mean"]):.1f}" cy="{y:.1f}" r="5"/>')
        parts.append(
            f'<text class="m" x="{x(p["mean"]) + 8:.1f}" y="{y - 6:.1f}" font-size="10">'
            f"{p['mean']:+.1f}</text>"
        )
    axis_y = top + row_h * len(items) + 18
    for v in (lo, (lo + hi) / 2, hi):
        parts.append(
            f'<text class="m" x="{x(v):.1f}" y="{axis_y}" font-size="10" '
            f'text-anchor="middle">{v:.0f}</text>'
        )
    parts.append(
        f'<text class="m" x="{(left + width - right) / 2:.1f}" y="{axis_y + 18}" font-size="11" '
        f'text-anchor="middle">{escape(unit)}</text>'
    )
    parts.append("</svg>")
    return "\n".join(parts)


def analyse(args) -> None:
    out = Path(args.out)
    (out / "figures").mkdir(parents=True, exist_ok=True)
    # the runs' source digest (default: this tree). A later analysis-only change to this module
    # changes the tree digest, so the report names the recorded digest it analyses explicitly.
    src = getattr(args, "source", None) or ev.source_digest()
    rows, meta = ev.load_rows(src)
    manifest = [
        json.loads(line)
        for line in (ev.RAW / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    manifest = [m for m in manifest if m["source_sha256"] == src]
    status = Counter(m["status"] for m in manifest)
    excluded_sources = sorted(meta["source"] - {src})
    design = ev.design_digest()

    flat = []
    for (scenario, arm, seed), m in sorted(rows.items()):
        flat.append(
            {
                "scenario": scenario,
                "arm": arm,
                "seed": seed,
                **{k: ev.METRICS[k][0](m) for k in HEADLINE},
                "deadline_censored": m["deadline_censored"],
                "selector_model": m["_selector"],
                "positive_budget_decisions": json.dumps(
                    m["_state"].get("positive_budget_decisions")
                )
                if arm.startswith("aorts")
                else "",
            }
        )
    with (out / "runs_flat.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(flat[0]))
        w.writeheader()
        w.writerows(flat)

    summary = {
        "source_sha256": src,
        "design_sha256": design,
        "design": ev.DESIGN,
        "runs_used": len(rows),
        "manifest_status": dict(status),
        "excluded_source_digests": excluded_sources,
        "scenarios": {},
    }
    md = [
        "# Stage 12 results (generated; do not edit by hand)",
        "",
        f"- source digest `{src}`; design digest `{design}`",
        f"- runs used {len(rows)}; manifest status {dict(status)}; rows from other source "
        f"digests excluded: {len(excluded_sources)}",
        "- engine: independent SimPy DES for every arm; scale "
        f"{ev.SCALE}; paired seeds per scenario as listed",
        "- cells: mean paired difference arm - reference (median), [95 % bootstrap CI of the "
        "mean] when n >= 10; negative is better for new ambiguous / unresolved / latency",
        "",
    ]
    for scenario, (_, imp, desc) in ev.SCENARIOS.items():
        arms = ev.arms_for(scenario)
        sc = {"description": desc, "impairment": bool(imp), "arms": {}, "vs_no_check": {},
              "aorts_vs": {}}  # fmt: skip
        for arm in arms:
            sc["arms"][arm] = aggregate(rows, scenario, arm)
            if arm != "no-check":
                sc["vs_no_check"][arm] = {
                    k: ev.paired(rows, scenario, arm, "no-check", k) for k in HEADLINE
                }
            if arm != "aorts":
                sc["aorts_vs"][arm] = {
                    k: ev.paired(rows, scenario, "aorts", arm, k) for k in HEADLINE
                }
        summary["scenarios"][scenario] = sc
        n = len(ev.seeds_for(scenario))
        md += [f"## {scenario}", "", f"{desc}. Paired seeds: {n}." + (
            " Impairment applies to AORTS arms only." if imp else ""), ""]  # fmt: skip
        md += [
            "| arm | checks | resolved by check | new ambiguous | unresolved at end "
            "| p95 / p99 pay (s) | deadline compl. | safety viol. |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for arm in arms:
            a = sc["arms"][arm]
            md.append(
                f"| {arm} | {_fmt(a['checks_released']['mean'], 1)} "
                f"| {_fmt(a['resolved_by_check']['mean'], 1)} "
                f"| {_fmt(a['new_ambiguous']['mean'], 1)} "
                f"| {_fmt(a['unresolved_at_end']['mean'], 1)} "
                f"| {_fmt(a['payment_latency_p95_s']['mean'])} / "
                f"{_fmt(a['payment_latency_p99_s']['mean'])} "
                f"| {_fmt(a['deadline_completion']['mean'], 3)} "
                f"| {_fmt(a['safety_violations']['mean'], 1)} |"
            )
        md += ["", "Paired difference vs no-check (check-induced ambiguity and recovery):", ""]
        md += ["| arm | new ambiguous induced | resolved by check | unresolved at end |",
               "|---|---|---|---|"]  # fmt: skip
        for arm, d in sc["vs_no_check"].items():
            md.append(
                f"| {arm} | {_ci(d['new_ambiguous'])} | {_ci(d['resolved_by_check'])} "
                f"| {_ci(d['unresolved_at_end'])} |"
            )
        md.append("")
        items = [(arm, sc["vs_no_check"][arm]["new_ambiguous"]) for arm in sc["vs_no_check"]]
        (out / "figures" / f"{scenario}-induced-ambiguity.svg").write_text(
            _svg_diffs(
                f"{scenario}: newly ambiguous payments, arm minus no-check",
                "mean paired difference per arm, 95 % bootstrap CI where n >= 10 (DES)",
                items,
                "payments (negative = fewer new ambiguous than doing no checks)",
            ),
            encoding="utf-8",
        )
        items = [(arm, sc["vs_no_check"][arm]["unresolved_at_end"]) for arm in sc["vs_no_check"]]
        (out / "figures" / f"{scenario}-unresolved.svg").write_text(
            _svg_diffs(
                f"{scenario}: unresolved ambiguous backlog at the end, arm minus no-check",
                "mean paired difference per arm, 95 % bootstrap CI where n >= 10 (DES)",
                items,
                "payments (negative = smaller backlog than doing no checks)",
            ),
            encoding="utf-8",
        )
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str), "utf-8")
    (out / "RESULTS.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(json.dumps({"runs_used": len(rows), "status": dict(status), "out": str(out)}))
