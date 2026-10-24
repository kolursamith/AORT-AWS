"""Stage 13: teacher demonstrations of the three AORTS attacks on the integrated system.

Each demonstration runs **real** integrated runs (Track B services in process with real HTTP
handlers and HMAC signing, real PostgreSQL, a real Track A controller) through the same
per-run functions as the Stage 10 campaigns, then narrates what happened from the recorded
ticks, requests and ledger checks, and draws SVG timelines from those ticks. The paired-seed
comparison across five conditions is quoted from the Stage 10 evidence package, not re-run.

    python -m backend.aorts.experiments.stage13_demo flooding  [--seed 101]
    python -m backend.aorts.experiments.stage13_demo poisoning [--seed 101]
    python -m backend.aorts.experiments.stage13_demo replay    [--seed 101]

Output: ``docs/demo/<attack>/`` (narration Markdown, run JSON, SVG timelines; small) and
per-tick raw files under ``results/processed/stage13/`` (not in Git). Synthetic data only.
"""

from __future__ import annotations

import argparse
import csv
import json
from html import escape
from pathlib import Path

from backend.aorts.experiments import stage10_security as s10
from backend.aorts.experiments.common import ROOT
from backend.aorts.experiments.security_package import write_json

OUT = ROOT / "docs" / "demo"
RAW = ROOT / "results" / "processed" / "stage13"
EVIDENCE = ROOT / "docs" / "integration" / "evidence" / "stage10"


# ----------------------------------------------------------------------------- SVG


def timeline_svg(title: str, subtitle: str, ticks: list[dict], series: dict, window=None) -> str:
    """Step lines of per-tick values (from the loop ticks) with the attack window shaded.

    ``series``: label -> function(tick) -> float | None. ``window``: (start_t, end_t) shaded.
    """

    width, height, left, right, top, bottom = 980, 340, 56, 270, 60, 40
    ts = [t["t"] for t in ticks]
    if not ts:
        return ""
    t0, t1 = min(ts), max(ts)
    values = {k: [f(t) for t in ticks] for k, f in series.items()}
    vmax = max([v for vs in values.values() for v in vs if v is not None] + [1.0])
    colours = ["#2a78d6", "#eb6834", "#1baf7a", "#8a5cd1"]
    dark = ["#3987e5", "#d95926", "#199e70", "#9b72de"]

    def x(t):
        return left + (t - t0) / max(t1 - t0, 1e-9) * (width - left - right)

    def y(v):
        return top + (1 - v / (vmax * 1.08)) * (height - top - bottom)

    css = ".bg{fill:#fcfcfb}.t{fill:#0b0b0b}.m{fill:#52514e}.g{stroke:#e4e3df}.w{fill:#eb6834}"
    css_dark = ".bg{fill:#1a1a19}.t{fill:#f0efec}.m{fill:#c3c2b7}.g{stroke:#3a3936}"
    for i, (c, d) in enumerate(zip(colours, dark, strict=True)):
        css += f".s{i}{{stroke:{c}}}.f{i}{{fill:{c}}}"  # lines: stroke only; legend: fill
        css_dark += f".s{i}{{stroke:{d}}}.f{i}{{fill:{d}}}"
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="system-ui, sans-serif">',
        f"<style>{css}@media (prefers-color-scheme: dark){{{css_dark}}}</style>",
        f'<rect class="bg" width="{width}" height="{height}"/>',
        f'<text class="t" x="16" y="24" font-size="15" font-weight="600">{escape(title)}</text>',
        f'<text class="m" x="16" y="44" font-size="12">{escape(subtitle)}</text>',
    ]
    if window:
        a, b = (max(t0, window[0]), min(t1, window[1]))
        parts.append(
            f'<rect class="w" x="{x(a):.1f}" y="{top}" width="{max(0, x(b) - x(a)):.1f}" '
            f'height="{height - top - bottom}" opacity="0.12"/>'
        )
        parts.append(
            f'<text class="m" x="{x(a) + 4:.1f}" y="{top + 12}" font-size="10">attack</text>'
        )
    for k in range(5):
        v = vmax * 1.08 * k / 4
        parts.append(
            f'<line class="g" x1="{left}" y1="{y(v):.1f}" x2="{width - right}" y2="{y(v):.1f}"/>'
        )
        parts.append(
            f'<text class="m" x="{left - 6}" y="{y(v) + 4:.1f}" font-size="10" '
            f'text-anchor="end">{v:.1f}</text>'
        )
    for i, (label, vs) in enumerate(values.items()):
        pts = []
        for t, v in zip(ts, vs, strict=True):
            if v is None:
                continue
            pts.append(f"{x(t):.1f},{y(v):.1f}")
        if pts:
            parts.append(
                f'<polyline class="s{i}" fill="none" stroke-width="2" '
                f'stroke-dasharray="{"6 4" if i % 2 else "none"}" points="{" ".join(pts)}"/>'
            )
        ly = top + 14 + 18 * i
        parts.append(
            f'<rect class="f{i}" x="{width - right + 12}" y="{ly - 8}" width="12" height="3"/>'
        )
        parts.append(
            f'<text class="t" x="{width - right + 30}" y="{ly - 3}" font-size="11">'
            f"{escape(label)}</text>"
        )
    parts.append(
        f'<text class="m" x="{(left + width - right) / 2:.0f}" y="{height - 10}" font-size="11" '
        'text-anchor="middle">seconds since the loop started (one point per controller tick)</text>'
    )
    parts.append("</svg>")
    return "\n".join(parts)


def _route(t, rid, key):
    return (t.get("routes", {}).get(rid) or {}).get(key)


def _num(v):
    return float(v) if isinstance(v, (int, float)) else None


def _evidence_table(kind: str, group_key: str, metrics: list[str]) -> list[str]:
    """Paired-seed means per condition, read from the newest Stage 10 runs CSV."""

    files = sorted((EVIDENCE / kind).glob("*-runs.csv"))
    if not files:
        return [f"_No Stage 10 {kind} evidence found._"]
    with files[-1].open(encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("status") == "completed"]
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r[group_key], []).append(r)
    out = [f"Source: `{files[-1].relative_to(ROOT).as_posix()}` (completed runs only).", "",
           "| condition | seeds | " + " | ".join(metrics) + " |",
           "|---|---|" + "---|" * len(metrics)]  # fmt: skip
    for g, rs in groups.items():
        cells = []
        for m in metrics:
            vals = [float(r[m]) for r in rs if r.get(m) not in (None, "", "None")]
            cells.append(f"{sum(vals) / len(vals):.2f}" if vals else "-")
        out.append(f"| {g} | {len(rs)} | " + " | ".join(cells) + " |")
    return out


# ------------------------------------------------------------------------ flooding


def demo_flooding(seed: int) -> list[str]:
    out, raw = OUT / "flooding", RAW / "flooding"
    runs = {}
    for cond in ("flood_undefended", "flood_defended"):
        outputs, row = s10.flood_once(cond, seed, s10.FLOOD_RATES[-1], f"demo-{cond}-s{seed}", raw)
        ticks = json.loads(Path(outputs["ticks"]).read_text())
        runs[cond] = {"row": row, "outputs": {k: str(v) for k, v in outputs.items()}}
        lead, dur = s10.TIMELINE["lead_s"], s10.TIMELINE["attack_s"]
        t_attack = row.get("warm_settle_s")  # informative only; window from the row below
        start = ticks[0]["t"] if ticks else 0
        # the attack starts after warm-up; the harness records the timeline window per tick phase
        timeline = [t for t in ticks if t.get("phase") == "timeline"]
        a0 = timeline[0]["t"] + lead if timeline else start
        (out / f"{cond}-timeline.svg").parent.mkdir(parents=True, exist_ok=True)
        (out / f"{cond}-timeline.svg").write_text(
            timeline_svg(
                f"Bank A3 status-check flooding ({cond.replace('_', ' ')}), seed {seed}",
                f"{s10.FLOOD_RATES[-1]:.0f} checks/s from A3 on route B; controller ticks "
                "of the integrated loop",
                timeline or ticks,
                {
                    "A1-B budget (checks/s)": lambda t: _num(_route(t, "A1-B", "budget_rps")),
                    "A1-B dispatched": lambda t: _num(_route(t, "A1-B", "dispatched")),
                    "A1-C budget (checks/s)": lambda t: _num(_route(t, "A1-C", "budget_rps")),
                    "unresolved backlog / 10": lambda t: _num(t.get("unresolved", 0)) / 10,
                },
                (a0, a0 + dur),
            ),
            encoding="utf-8",
        )
        del t_attack
    write_json(out / f"runs-s{seed}.json", runs)
    u, d = runs["flood_undefended"]["row"], runs["flood_defended"]["row"]
    return [
        "# Demo 1: compromised bank A3 floods status checks",
        "",
        "**Threat.** A3 is a valid switch client but sends status inquiries far above its share "
        f"(here {s10.FLOOD_RATES[-1]:.0f}/s for {s10.TIMELINE['attack_s']:.0f} s) on the route "
        "that A1/A2 legitimately use (B).",
        "",
        "**Trigger.** Track B's own A3 flooding generator (`run_flood`) against the in-process "
        "switch, while the integrated AORTS loop keeps running (telemetry -> controller -> "
        "execution gate -> checks).",
        "",
        f"| measured in this run (seed {seed}) | defences OFF | defences ON |",
        "|---|---|---|",
        f"| legitimate (A1/A2) check success during attack | {u['legit_check_success_during']}"
        f" | {d['legit_check_success_during']} |",
        f"| legitimate check p95 latency during (ms) | {u['legit_check_p95_ms_during']} "
        f"| {d['legit_check_p95_ms_during']} |",
        f"| A3 checks accepted / rejected (429) | {u['A3_accepted']} / {u['A3_rejected']} "
        f"| {d['A3_accepted']} / {d['A3_rejected']} |",
        f"| A3 rejections: RATE_LIMITED / CREDIT_EXHAUSTED | {u['A3_rejected_RATE_LIMITED']} / "
        f"{u['A3_rejected_CREDIT_EXHAUSTED']} | {d['A3_rejected_RATE_LIMITED']} / "
        f"{d['A3_rejected_CREDIT_EXHAUSTED']} |",
        f"| detection / containment (s) | {u['detection_s']} / {u['containment_s']} "
        f"| {d['detection_s']} / {d['containment_s']} |",
        f"| route B payments closed per s during | {u['payments_closed_per_s_B_during']} "
        f"| {d['payments_closed_per_s_B_during']} |",
        f"| payment p95 on B during (ms) | {u['payment_p95_ms_B_during']} "
        f"| {d['payment_p95_ms_B_during']} |",
        f"| controller budget on A1-B before / during | {u['B_budget_mean_before']} / "
        f"{u['B_budget_mean_during']} | {d['B_budget_mean_before']} / "
        f"{d['B_budget_mean_during']} |",
        f"| controller recovery after attack (s) | {u['controller_B_recovery_s']} "
        f"| {d['controller_B_recovery_s']} |",
        f"| ledger integrity / duplicate checks / second debits | {u['ledger_ok']} / "
        f"{u['duplicate_check_starts']} / {u['multiple_debits']} | {d['ledger_ok']} / "
        f"{d['duplicate_check_starts']} / {d['multiple_debits']} |",
        "",
        "**Detection and enforcement.** Switch admission (per-bank token bucket, route and "
        "global limits, Tier-2 credits) rejects A3 before the worker pool; the controller "
        "sees the congestion only through the remitter's own telemetry and lowers or holds its "
        "budget; Track B's execution gate admits only what the decision and quotas allow.",
        "",
        "Timelines (from the recorded ticks): `flood_undefended-timeline.svg`, "
        "`flood_defended-timeline.svg`.",
        "",
        "## Paired-seed evidence (Stage 10 campaign, five conditions, not re-run here)",
        "",
        *_evidence_table(
            "flooding",
            "group",
            [
                "legit_check_success_during",
                "legit_check_p95_ms_during",
                "A3_accepted",
                "payments_closed_per_s_B_during",
                "controller_B_recovery_s",
                "unresolved_end",
            ],
        ),  # fmt: skip
    ]


# ----------------------------------------------------------------------- poisoning


def demo_poisoning(seed: int) -> list[str]:
    out, raw = OUT / "poisoning", RAW / "poisoning"
    runs = {}
    for arm in s10.ARMS:
        outputs, row = s10.poison_once("inflate_100", arm, seed, f"demo-{arm}-s{seed}", raw)
        ticks = json.loads(Path(outputs["ticks"]).read_text())
        runs[arm] = {"row": row, "outputs": {k: str(v) for k, v in outputs.items()}}
        before = [t for t in ticks if t.get("phase") == "before"]
        a0 = (before[-1]["t"] + 1.0) if before else ticks[0]["t"]
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{arm}-timeline.svg").write_text(
            timeline_svg(
                f"Telemetry poisoning (+100 % throughput), {arm.replace('_', ' ')}, seed {seed}",
                "route B capacity drops at attack start in every arm; A1-B's source lies "
                "(except in the clean arm)",
                [t for t in ticks if t.get("phase") in ("before", "during", "after")],
                {
                    "A1-B budget (checks/s)": lambda t: _num(_route(t, "A1-B", "budget_rps")),
                    "A1-B safe headroom": lambda t: _num(_route(t, "A1-B", "headroom_rps")),
                    "A1-B flagged (1 = not TRUSTED)": lambda t: (
                        None
                        if _route(t, "A1-B", "verdict") is None
                        else float(_route(t, "A1-B", "verdict") != "TRUSTED")
                    ),
                    "A1-B dispatched": lambda t: _num(_route(t, "A1-B", "dispatched")),
                },
                (a0, a0 + 12.0),
            ),
            encoding="utf-8",
        )
    write_json(out / f"runs-s{seed}.json", runs)
    keys = ["B_detection_rate", "B_detection_tick_offset", "B_positive_budget_ticks_during",
            "B_max_budget_during", "B_dispatched_during", "B_payment_p95_ms_during",
            "C_dispatched_during", "ledger_ok", "unverified_evidence_applied"]  # fmt: skip
    lines = [
        "# Demo 2: digital-twin telemetry poisoning",
        "",
        "**Threat.** The remitter-side telemetry source for route A1-B reports twice its real "
        "throughput while route B's capacity has just dropped unannounced (capacity 1 worker, "
        "+500 ms at the beneficiary). Goal: make the controller believe there is headroom and "
        "spend scarce capacity on checks.",
        "",
        "**Trigger.** `poison_once('inflate_100', arm, seed)`: the producer's honest report is "
        "replaced by the lie for 12 s; the switch's independent measurement is untouched.",
        "",
        f"| measured in this run (seed {seed}) | " + " | ".join(s10.ARMS) + " |",
        "|---|" + "---|" * len(s10.ARMS),
    ]
    for k in keys:
        lines.append(f"| {k} | " + " | ".join(str(runs[a]["row"].get(k)) for a in s10.ARMS) + " |")
    lines += [
        "",
        "**Detection and response.** The Track B validator cross-checks the reported "
        "throughput against the switch's own per-route measurement (plus EWMA/CUSUM); flagged "
        "windows never reach the twin as trusted evidence, and the Stage 06 estimator gives "
        "positive headroom only on a HEALTHY route with a conclusive conservation check. In "
        "the `poisoned_unprotected` arm the validator is disabled (research arm only; ledger "
        "protections unchanged), so whatever budget remains is the estimator's own defence.",
        "",
        "**Caveat (shown, not hidden).** The validator also flags most windows in the *clean* "
        "arm: the unannounced capacity drop makes the honest report deviate from the switch's "
        "measurement too. A flag means 'not trusted', not 'proven lie'. What the comparison "
        "shows is the extra positive-budget ticks when the validator is off, and that "
        "dispatches stayed bounded in every arm.",
        "",
        "Timelines: `clean-timeline.svg`, `poisoned_unprotected-timeline.svg`, "
        "`poisoned_protected-timeline.svg`.",
        "",
        "## Paired-seed evidence (Stage 10 campaign, not re-run here)",
        "",
        *_evidence_table("poisoning", "condition", s10.POISON_METRICS),
    ]
    return lines


# -------------------------------------------------------------------------- replay


def demo_replay(seed: int) -> list[str]:
    out, raw = OUT / "replay", RAW / "replay"
    outputs, row = s10.replay_once(seed, True, f"demo-replay-s{seed}", raw)
    cases = json.loads(Path(outputs["v11_cases"]).read_text())
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / f"run-s{seed}.json", {"row": row, "v11_cases": cases,
                                           "outputs": {k: str(v) for k, v in outputs.items()}})  # fmt: skip
    lines = [
        "# Demo 3: transaction replay",
        "",
        "**Threat.** An attacker (or a faulty network) re-sends payments, status answers, "
        "events and reversal notifications, forges or re-uses signed evidence, or answers for "
        "the wrong transaction or route, hoping for a second debit, credit or reversal, or a "
        "terminal-state regression.",
        "",
        "**Trigger.** Track B's replay simulator plus the v1.1 replay cases against the "
        f"integrated stack on PostgreSQL (seed {seed}).",
        "",
        f"Track B replay cases: {row['trackb_cases']}; duplicate reversal postings: "
        f"{row['trackb_duplicate_reversal_postings']}. v1.1 cases: {row['v11_cases']}; "
        f"unexpected outcomes: {row['v11_unexpected']}.",
        "",
        "| v1.1 case | expected | as expected |",
        "|---|---|---|",
        *[f"| {c['case']} | {escape(str(c['expected']))} | {c['as_expected']} |" for c in cases],
        "",
        f"Ledger after the attack: integrity {row['ledger_ok']}, duplicate check starts "
        f"{row['duplicate_check_starts']}, second debits {row['multiple_debits']}, second "
        f"reversals {row['multiple_reversals']}, unverified evidence applied "
        f"{row['unverified_evidence_applied']}.",
        "",
        "**Enforcement.** Idempotency keys and unique operation ids (one debit), row-locked "
        "version CAS (no stale or concurrent second effect), HMAC response verification with "
        "key identity and a ±300 s window (forged/expired evidence is stored but never applied), "
        "controller intake idempotency and conflict detection (duplicate events change nothing).",
        "",
        "## Paired-seed evidence (Stage 10 campaign, not re-run here)",
        "",
        *_evidence_table(
            "replay", "condition", ["v11_unexpected", "trackb_duplicate_reversal_postings"]
        ),  # fmt: skip
    ]
    return lines


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description="AORTS Stage 13 security demonstrations")
    p.add_argument("attack", choices=["flooding", "poisoning", "replay"])
    p.add_argument("--seed", type=int, default=101)
    args = p.parse_args(argv)
    lines = {"flooding": demo_flooding, "poisoning": demo_poisoning, "replay": demo_replay}[
        args.attack
    ](args.seed)
    target = OUT / args.attack / "README.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    header = [
        "<!-- generated by python -m backend.aorts.experiments.stage13_demo; real runs -->",
        "",
    ]
    target.write_text("\n".join(header + lines) + "\n", encoding="utf-8")
    print(f"wrote {target}")


if __name__ == "__main__":  # pragma: no cover
    main()
