"""PAGE 6: Security operations center (recorded Stage 10 evidence + live counters)."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from backend.aorts.dashboard.command_center import components as ui
from backend.aorts.dashboard.command_center import sources as src
from backend.aorts.dashboard.command_center.theme import INK

OFF, ON = INK["red"], INK["emerald"]


def _bars(df: pd.DataFrame, x: str, ys: dict[str, str], title: str, height: int = 260):
    fig = go.Figure()
    for (label, col), colour in zip(ys.items(), [INK["cyan"], INK["violet"], INK["amber"]],
                                    strict=False):  # fmt: skip
        fig.add_bar(x=df[x], y=df[col], name=label, marker_color=colour)
    fig.update_layout(barmode="group", height=height, title=title)
    return fig


def flooding() -> None:
    df, err = src.stage10_runs("flooding")
    if err:
        ui.empty("Stage 10 flooding evidence", err)
        return
    ui.note("<b>Threat.</b> Bank A3 is a valid switch client that floods status inquiries on route "
            "B, which A1/A2 also use. Track B's switch admission (per-bank token bucket, route and "
            "global limits, Tier-2 credits) is the defence; the AORTS controller only ever sees "
            "the congestion through the remitter's own telemetry.")  # fmt: skip
    rate = st.select_slider("A3 attack rate (checks/s)", options=[10, 50, 100, 150], value=150)
    top = df[df.attack_rps.isin([rate, 0])]
    agg = top.groupby("condition").mean(numeric_only=True).reset_index()
    order = ["normal", "flood_undefended", "flood_defended", "parallel_route_undefended",
             "parallel_route_defended"]  # fmt: skip
    agg["condition"] = pd.Categorical(agg.condition, [o for o in order if o in set(agg.condition)])
    agg = agg.sort_values("condition")
    u = agg[agg.condition == "flood_undefended"]
    d = agg[agg.condition == "flood_defended"]
    if not u.empty and not d.empty:
        u, d = u.iloc[0], d.iloc[0]
        ui.kpi_row([
            ui.kpi("Legit check success", f"{u.legit_check_success_during:.2f} → "
                   f"{d.legit_check_success_during:.2f}", "defence OFF → ON (during attack)", "emerald"),
            ui.kpi("Legit check p95", f"{u.legit_check_p95_ms_during:.0f} → {d.legit_check_p95_ms_during:.0f} ms",
                   "defence OFF → ON", "emerald"),
            ui.kpi("A3 checks admitted", f"{u.A3_accepted:.0f} → {d.A3_accepted:.0f}", f"of {u.A3_sent:.0f} sent"),
            ui.kpi("A3 rate-limited", f"{d.A3_rejected_RATE_LIMITED:.0f}", "429 RATE_LIMITED (defence ON)", "red"),
            ui.kpi("Containment", f"{d.containment_s:.1f} s" if pd.notna(d.containment_s) else "—",
                   f"detection {d.detection_s:.2f} s" if pd.notna(d.detection_s) else ""),
        ])  # fmt: skip
    c1, c2 = st.columns(2)
    c1.plotly_chart(_bars(agg, "condition", {"before": "legit_check_success_before",
                                             "during attack": "legit_check_success_during",
                                             "after attack": "legit_check_success_after"},
                          "Legitimate (A1/A2) check success"), use_container_width=True)  # fmt: skip
    c2.plotly_chart(_bars(agg, "condition", {"p95 during": "legit_check_p95_ms_during",
                                             "p99 during": "legit_check_p99_ms_during"},
                          "Legitimate check latency (ms)"), use_container_width=True)  # fmt: skip
    c1, c2 = st.columns(2)
    c1.plotly_chart(_bars(agg, "condition", {"A3 admitted": "A3_accepted", "A3 rejected (429)": "A3_rejected",
                                             "A3 reached workers": "A3_reached_worker_pool"},
                          "Attacker admission"), use_container_width=True)  # fmt: skip
    c2.plotly_chart(_bars(agg, "condition", {"route B": "payment_p95_ms_B_during",
                                             "route C (healthy)": "payment_p95_ms_C_during"},
                          "Payment p95 during attack (ms)"), use_container_width=True)  # fmt: skip
    ui.note("<b>Finding kept.</b> Without defences the healthy parallel route C also degrades: "
            "checks share the switch's core worker pool in this profile. Payments kept closing at "
            "the offered rate in every arm; ledger checks were clean in all 55 runs.", "warn")  # fmt: skip
    st.caption(f"RECORDED: {df.attrs.get('source', 'docs/integration/evidence/stage10')} · {len(df)} completed runs, seeds "
               f"{sorted(df.seed.unique().tolist())} · mean over paired seeds")  # fmt: skip


def poisoning() -> None:
    df, err = src.stage10_runs("poisoning")
    if err:
        ui.empty("Stage 10 poisoning evidence", err)
        return
    ui.note("<b>Threat.</b> The remitter-side telemetry source lies (throughput +30 % or +100 %, "
            "low latency, zero timeouts, inconsistent backlog, conflicting values) while route B's "
            "capacity drops unannounced. <b>Anomalous ≠ malicious:</b> the validator flags windows "
            "that disagree with the switch's own measurement. A real capacity drop is flagged too, "
            "so a flag means <i>not trusted</i>, not <i>proven lie</i>.")  # fmt: skip
    runs = df[df.condition != "protocol"]
    case = st.selectbox("Manipulation", sorted(runs.case.dropna().unique()),
                        index=sorted(runs.case.dropna().unique()).index("inflate_100"))  # fmt: skip
    sub = runs[runs.case == case].groupby("arm").mean(numeric_only=True).reset_index()
    sub["arm"] = pd.Categorical(sub.arm, ["clean", "poisoned_unprotected", "poisoned_protected"])
    sub = sub.sort_values("arm")
    c1, c2 = st.columns(2)
    c1.plotly_chart(_bars(sub, "arm", {"windows flagged (share)": "B_detection_rate"},
                          "Validator flags on route B during the attack"), use_container_width=True)  # fmt: skip
    c2.plotly_chart(_bars(sub, "arm", {"positive-budget ticks": "B_positive_budget_ticks_during",
                                       "max headroom /s": "B_max_headroom_during",
                                       "checks dispatched": "B_dispatched_during"},
                          "Controller response on route B"), use_container_width=True)  # fmt: skip
    proto = df[df.condition == "protocol"]
    if not proto.empty:
        codes = {
            c.split(":")[0]: proto[c].iloc[0] for c in proto.columns if c.endswith(":error_code")
        }
        ui.section("Protocol-level forgeries", "sent through the controller's real intake")
        st.dataframe(pd.DataFrame([{"forgery": k, "refused with": v,
                                    "applied": int(proto.protocol_applied.max())} for k, v in codes.items()]),
                     hide_index=True, use_container_width=True)  # fmt: skip
        ui.note(f"Source silent 10 s → controller answers <code>{proto.silent_last_reason.iloc[0]}</code>"
                f" (fail closed), fallback dispatched {int(proto.silent_fallback_dispatched.max())}; "
                f"first positive budget {proto.return_time_to_positive_s.mean():.1f} s after "
                "observations return.")  # fmt: skip
    st.caption(
        f"RECORDED: {df.attrs.get('source', 'docs/integration/evidence/stage10')} · {len(df)} completed runs · mean over 5 seeds"
    )


def replay() -> None:
    df, err = src.stage10_runs("replay")
    cases, cerr = src.doc_json("demo/replay/run-s101.json")
    ui.note("<b>Threat.</b> Re-sent payments, status answers, events and reversal notifications; "
            "forged, expired or foreign signed evidence. Goal: a second debit, credit or reversal, "
            "or a terminal-state regression.")  # fmt: skip
    if err:
        ui.empty("Stage 10 replay evidence", err)
    else:
        ui.kpi_row([
            ui.kpi("Replay runs", len(df), "5 seeds × auto-reverse on/off", "cyan"),
            ui.kpi("Unexpected outcomes", int(df.v11_unexpected.sum()), "13 v1.1 cases per run",
                   "emerald" if df.v11_unexpected.sum() == 0 else "red"),
            ui.kpi("Duplicate reversal postings", int(df.trackb_duplicate_reversal_postings.sum()),
                   "23 Track B cases per run", "emerald"),
            ui.kpi("Second debits", int(df.multiple_debits.sum()), "ledger check", "emerald"),
            ui.kpi("Unverified evidence applied", int(df.unverified_evidence_applied.sum()),
                   "ledger check", "emerald"),
        ])  # fmt: skip
    if cases:
        ui.section("Case by case (Stage 13 demo run, seed 101, PostgreSQL)", "")
        st.dataframe(pd.DataFrame([{"case": c["case"], "expected": c["expected"],
                                    "as expected": c["as_expected"]} for c in cases["v11_cases"]]),
                     hide_index=True, use_container_width=True)  # fmt: skip
    else:
        ui.empty("demo replay run", cerr)
    st.caption(
        "RECORDED: docs/integration/evidence/stage10/replay · docs/demo/replay/run-s101.json"
    )


def render() -> None:
    ui.hero("SECURITY OPERATIONS CENTER", "Three attacks on the integrated system",
            "Comparisons come from the Stage 10 campaign: 165 real integrated runs on PostgreSQL "
            "(5 paired seeds per condition). Live counters come from the running stack.",
            ["RECORDED", "LIVE"])  # fmt: skip
    a, b, c, live = st.tabs(["A · Status-check flooding", "B · Telemetry poisoning",
                             "C · Transaction replay", "Live security counters"])  # fmt: skip
    with a:
        flooding()
    with b:
        poisoning()
    with c:
        replay()
    with live:
        s, err = src.v11_summary()
        if err:
            ui.empty("live security summary", err)
            return
        c1, c2 = st.columns(2)
        with c1:
            ui.section("Per-bank status traffic", "switch counters (process memory)")
            pb = s.get("per_bank_checks") or {}
            if pb:
                st.dataframe(pd.DataFrame([{"bank": k, **{x: y for x, y in v.items()
                                                          if x != "rejected_by_reason"}} for k, v in pb.items()]),
                             hide_index=True, use_container_width=True)  # fmt: skip
            else:
                ui.no_data("No status checks since the switch started", "")
            ui.section("Telemetry trust", "validator verdicts, latest windows")
            st.json(s.get("telemetry"), expanded=False)
        with c2:
            ui.section("Replay / authentication rejections", "audit log and dead letters")
            st.json(s.get("replay_rejections"), expanded=True)
            ui.section("Financial safety", "ledger checks")
            st.json(s.get("financial_safety"), expanded=True)
