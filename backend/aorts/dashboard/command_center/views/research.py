"""PAGE 7: Research analytics (Stage 12 final evaluation, recorded)."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from backend.aorts.dashboard.command_center import components as ui
from backend.aorts.dashboard.command_center import sources as src
from backend.aorts.dashboard.command_center.theme import INK, SERIES

METRICS = {
    "new_ambiguous": ("Newly ambiguous payments", "lower is better"),
    "resolved_by_check": ("Authoritative resolutions by check", "higher is better"),
    "unresolved_at_end": ("Unresolved backlog at end", "lower is better"),
    "checks_released": ("Status-check traffic (checks released)", "cost"),
    "payment_latency_p95_s": ("Payment latency p95 (s)", "lower is better"),
    "payment_latency_p99_s": ("Payment latency p99 (s)", "lower is better"),
    "recovery_goodput": ("Recovery goodput (answers per check)", "higher is better"),
    "time_to_recovery_s": ("Time to recovery after fault (s)", "lower is better"),
}


def render() -> None:
    ui.hero("RESEARCH ANALYTICS", "AORTS against the Stage 05 baselines and its own ablations",
            "FINAL EVALUATION EVIDENCE · Stage 12 · 2,100 independent-DES runs, 13 scenarios, "
            "paired seeds (30 for key scenarios, 10 otherwise), 0 failed. Synthetic 1/50-scale "
            "workloads; no statistical-significance claim.", ["RECORDED"])  # fmt: skip
    summary, flat, err = src.stage12()
    if err:
        ui.empty("Stage 12 results", err)
        return
    ui.note("<b>Headline (negative finding, shown openly).</b> AORTS never increased new "
            "ambiguity against no-check in any scenario, but where status checks help (lost "
            "confirmations) it resolved 2.3 payments per run against AIMD's 2,278. The binding "
            "constraint is the conservative uncertainty bound: the no-uncertainty ablation "
            "resolved 476 with no added ambiguity. The feedback and look-ahead ablations showed "
            "no measurable effect in these designs.", "neg")  # fmt: skip
    scen = summary["scenarios"]
    c1, c2 = st.columns([2, 3])
    sc = c1.selectbox("Scenario", list(scen), index=list(scen).index("lost_confirmation"))
    metric = c2.selectbox("Metric", list(METRICS), format_func=lambda m: METRICS[m][0])
    s = scen[sc]
    st.caption(f"{s['description']} · " + ("impairment applies to AORTS arms only · " if
               s["impairment"] else "") + f"{len(s['arms'])} arms")  # fmt: skip
    arms = list(s["arms"])
    means = [s["arms"][a][metric]["mean"] for a in arms]
    left, right = st.columns(2)
    with left:
        ui.section(METRICS[metric][0], f"mean over paired seeds · {METRICS[metric][1]}")
        if all(m is None for m in means):
            ui.no_data("Not measurable in this design",
                       "e.g. deadlines all censored, or no fault to recover from")  # fmt: skip
        else:
            colours = [INK["cyan"] if a == "aorts" else INK["violet"] if a.startswith("aorts")
                       else INK["slate"] for a in arms]  # fmt: skip
            fig = go.Figure(go.Bar(x=arms, y=means, marker_color=colours))
            fig.update_layout(height=330, xaxis_tickangle=-35)
            st.plotly_chart(fig, use_container_width=True)
    with right:
        ui.section("Paired difference vs no-check", "95 % bootstrap CI (n ≥ 10)")
        rows = []
        for a, d in s["vs_no_check"].items():
            p = d.get(metric)
            if not p:
                continue
            ci = p.get("ci95_bootstrap") or [None, None]
            rows.append({"arm": a, "mean": p["mean"], "lo": ci[0], "hi": ci[1], "n": p["n"]})
        if not rows:
            ui.no_data("No paired values for this metric", "")
        else:
            d = pd.DataFrame(rows)
            fig = go.Figure(go.Scatter(
                x=d["mean"], y=d.arm, mode="markers",
                marker=dict(size=10, color=[INK["cyan"] if a == "aorts" else INK["violet"]
                                            if a.startswith("aorts") else INK["slate"] for a in d.arm]),
                error_x=dict(type="data", symmetric=False, array=(d.hi - d["mean"]).fillna(0),
                             arrayminus=(d["mean"] - d.lo).fillna(0), color=INK["muted"])))  # fmt: skip
            fig.add_vline(x=0, line_dash="dot", line_color=INK["muted"])
            fig.update_layout(height=330, xaxis_title="arm − no-check (same workload)")
            st.plotly_chart(fig, use_container_width=True)
    ui.section("Seed explorer", "every run is a recorded DES run")
    runs = flat[flat.scenario == sc]
    seed = st.select_slider("Seed", options=sorted(runs.seed.unique()))
    one = runs[runs.seed == seed][["arm", "checks_released", "resolved_by_check", "new_ambiguous",
                                   "unresolved_at_end", "payment_latency_p95_s",
                                   "selector_model", "positive_budget_decisions"]]  # fmt: skip
    one = one.rename(columns={"selector_model": "selector verdict (DES used)"})
    st.dataframe(one, hide_index=True, use_container_width=True)
    ui.section("Across scenarios", "full AORTS vs AIMD vs no-check, new ambiguity and resolutions")
    rows = []
    for name, v in scen.items():
        for a in ("no-check", "aimd", "aorts"):
            if a in v["arms"]:
                rows.append({"scenario": name, "arm": a,
                             "new ambiguous": v["arms"][a]["new_ambiguous"]["mean"],
                             "resolved by check": v["arms"][a]["resolved_by_check"]["mean"]})  # fmt: skip
    df = pd.DataFrame(rows)
    c1, c2 = st.columns(2)
    for col, m in ((c1, "new ambiguous"), (c2, "resolved by check")):
        fig = go.Figure()
        for i, a in enumerate(("no-check", "aimd", "aorts")):
            d = df[df.arm == a]
            fig.add_bar(x=d.scenario, y=d[m], name=a, marker_color=SERIES[[12, 4, 0][i]])
        fig.update_layout(barmode="group", height=330, title=m, xaxis_tickangle=-35)
        col.plotly_chart(fig, use_container_width=True)
    ui.note("<b>Not measurable here (stated, not hidden).</b> Deadline completion: every deadline "
            "(60 min) lies beyond the 25-40 min horizons, so all payments are censored. Bank "
            "fairness (Jain): every design has one remitter bank; fairness under attack is measured "
            "in Stage 10 flooding instead.", "warn")  # fmt: skip
    st.caption(f"RECORDED: docs/research/stage12 (summary.json, runs_flat.csv) · source digest "
               f"{summary['source_sha256'][:12]}… · design {summary['design_sha256'][:12]}… · "
               f"{summary['runs_used']} runs · {summary['manifest_status']}")  # fmt: skip
    text = src.doc_text("research/stage12/FINDINGS.md")
    if text:
        with st.expander("Full findings (docs/research/stage12/FINDINGS.md)"):
            st.markdown(text)
