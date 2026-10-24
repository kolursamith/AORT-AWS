"""PAGE 3: Route Digital Twin + Stage 06 capacity estimator (live)."""

from __future__ import annotations

import json

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from backend.aorts.dashboard.command_center import components as ui
from backend.aorts.dashboard.command_center import sources as src
from backend.aorts.dashboard.command_center.theme import INK

REASONS = {
    "CAPACITY_NOT_IDENTIFIABLE": "the route is not saturated, so only a lower bound on capacity "
    "exists (served work), not a point estimate",
    "BACKGROUND_LOAD_UNOBSERVABLE": "other banks' traffic on the same bottleneck is invisible to "
    "remitter-own telemetry",
    "SERVICE_DEFICIT": "the route served less than its own offered load in the latest block",
    "CONSISTENCY_UNVERIFIED": "the conservation check was not conclusive on the newest window",
    "STALE_TELEMETRY": "no fresh trusted window",
    "FLAGGED_TELEMETRY_NEWER": "newer windows were flagged by the validator and are not used",
    "TWIN_REPORTS_STALE": "the Route Twin reports its newest trusted observation as stale",
}


def _f(v, nd=2, unit=""):
    return "—" if v is None else f"{v:,.{nd}f}{unit}"


def render() -> None:
    ui.hero("ROUTE DIGITAL TWIN", "Trusted telemetry → capacity estimate → safe headroom",
            "The Route Twin keeps only AUTHENTICATED, unflagged windows from registered sources; "
            "the Stage 06 estimator derives a conservative capacity bound from them. Confidence "
            "is a heuristic score, not a calibrated probability.", ["LIVE"])  # fmt: skip
    view, err = src.controller()
    routes = list((view or {}).get("routes", {}).keys()) or ["A1-B", "A1-C"]
    route = st.radio("Route", [r for r in routes if r in ("A1-B", "A1-C")] or routes,
                     horizontal=True)  # fmt: skip
    if err:
        ui.empty("controller twin and estimate", err)
    else:
        r = view["routes"][route]
        snap, est = r["snapshot"], r["estimate"]
        tel = snap.get("latest_trusted_observation") or {}
        ui.kpi_row([
            ui.kpi("Estimator mode", (est or {}).get("mode"), "Stage 06", "cyan"),
            ui.kpi("Route state", (est or {}).get("route_state"), "hysteresis"),
            ui.kpi("Capacity lower bound", _f((est or {}).get("capacity_lower_bound_rps"), 2,
                                               " /s"), "conservative (served work)"),
            ui.kpi("Point estimate", _f((est or {}).get("estimated_capacity_rps"), 2, " /s"),
                   "only under saturation"),
            ui.kpi("Safe headroom", _f((est or {}).get("safe_headroom_rps"), 2, " /s"),
                   "for discretionary checks", "emerald" if (est or {}).get(
                       "safe_headroom_rps") else "amber"),
        ])  # fmt: skip
        st.write("")
        ui.kpi_row([
            ui.kpi("Offered load", _f((est or {}).get("offered_load_rps"), 2, " /s"), "EWMA"),
            ui.kpi("Observed throughput", _f(tel.get("observed_throughput_rps"), 2, " /s"),
                   "latest trusted window"),
            ui.kpi("p95 latency", _f(tel.get("p95_latency_ms"), 0, " ms"), "own traffic"),
            ui.kpi("Timeout rate", _f(tel.get("timeout_rate"), 3), "own traffic"),
            ui.kpi("Freshness", snap.get("freshness_status"), f"twin v{snap.get('snapshot_version')}"),
        ])  # fmt: skip
        st.write("")
        c1, c2 = st.columns([2, 3])
        with c1:
            ui.section("Trust and uncertainty", "")
            st.markdown(
                f"Confidence (heuristic, **uncalibrated**): "
                f"`{_f((est or {}).get('confidence'), 2)}` · usable samples "
                f"`{(est or {}).get('usable_samples')}` · rejected `{(est or {}).get('rejected_samples')}`"
                f" · identifiability `{(est or {}).get('identifiability')}`"
            )  # fmt: skip
            for code in (est or {}).get("reason_codes") or []:
                st.markdown(f"- `{code}`: {REASONS.get(code, '')}")
        with c2:
            ui.section("Twin snapshot", ui.when(snap.get("last_observed_at")))
            st.json({k: snap.get(k) for k in ("route_id", "freshness_status", "snapshot_version",
                                               "last_observed_at", "latest_sequence_per_source",
                                               "latest_flagged_observation")}, expanded=False)  # fmt: skip

    ui.section("Telemetry history", "remitter-reported throughput; validator cross-check verdicts")
    win, werr = src.query(
        """SELECT window_end, sequence, verdict, cross_check, measurement::text AS m
           FROM v11_telemetry_windows WHERE experiment_id = :e AND route_id = :r
           ORDER BY id DESC LIMIT 300""",
        e=src.EXP,
        r=route,
    )
    if werr or win is None or win.empty:
        ui.empty("telemetry windows", werr or "no windows yet")
        return
    rows = []
    for w in win.itertuples():
        m = json.loads(w.m)
        rep = m.get("reported") or {}
        rows.append({"t": w.window_end, "reported": rep.get("throughput_tps"),
                     "p95": rep.get("latency_p95_ms"), "verdict": w.verdict,
                     "cross": w.cross_check})  # fmt: skip
    hist = pd.DataFrame(rows).sort_values("t")
    fig = go.Figure()
    fig.add_scatter(x=hist.t, y=hist.reported, name="remitter-reported throughput",
                    line=dict(color=INK["cyan"], width=2))  # fmt: skip
    fig.add_scatter(x=hist.t, y=hist.p95, name="p95 latency (ms, right axis)", yaxis="y2",
                    line=dict(color=INK["violet"], width=1.5, dash="dot"))  # fmt: skip
    bad = hist[hist.verdict != "TRUSTED"]
    fig.add_scatter(x=bad.t, y=bad.reported, mode="markers", name="validator flagged",
                    marker=dict(color=INK["amber"], size=8, symbol="x"))  # fmt: skip
    fig.update_layout(
        height=320,
        yaxis_title="payments / s",
        yaxis2=dict(overlaying="y", side="right", showgrid=False, title="ms"),
    )
    st.plotly_chart(fig, use_container_width=True)
    cnt = hist.verdict.value_counts().to_dict()
    cc = hist.cross.value_counts().to_dict()
    st.caption(f"Latest {len(hist)} windows: " + ", ".join(f"{k} {v}" for k, v in cnt.items())
               + " · switch cross-check: " + ", ".join(f"{k} {v}" for k, v in cc.items())
               + ". The switch's own measurement is compared by the validator at window close; "
               "only its verdict is stored.")  # fmt: skip
    if view:
        ui.section("Route comparison", "A1-B (shared with the attacker in demos) vs A1-C")
        comp = []
        for rid, r in view["routes"].items():
            if rid not in ("A1-B", "A1-C"):
                continue
            e = r["estimate"] or {}
            comp.append({"route": rid, "mode": e.get("mode"), "state": e.get("route_state"),
                         "lower bound /s": e.get("capacity_lower_bound_rps"),
                         "headroom /s": e.get("safe_headroom_rps"),
                         "load /s": e.get("offered_load_rps"),
                         "freshness": r["snapshot"].get("freshness_status")})  # fmt: skip
        st.dataframe(pd.DataFrame(comp), hide_index=True, use_container_width=True)
    ui.source_line("LIVE: controller /v1.1/introspection (Route Twin + Stage 06 estimator)",
                   "v11_telemetry_windows (Track B validator)")  # fmt: skip
