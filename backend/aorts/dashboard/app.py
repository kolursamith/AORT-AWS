"""AORTS Track B operations & security dashboard (Streamlit).

Run locally:  streamlit run aorts/dashboard/app.py
Everything shown is from a SYNTHETIC mock environment. Measured values and the
controller's predictions are labelled separately.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from backend.aorts.dashboard import security_evidence as ev
from backend.aorts.dashboard.api_client import ControlClient

# Reference palette (validated): categorical slots 1-3 and fixed status colours.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]
STATUS = {"good": "#0ca30c", "warning": "#fab219", "critical": "#d03b3b"}
GRID = "rgba(128,128,128,0.18)"

st.set_page_config(page_title="AORTS Track B", page_icon=":bank:", layout="wide")
CONTROL_URL = os.environ.get("AORTS_CONTROL_URL", "http://127.0.0.1:8130")
ADMIN_TOKEN = os.environ.get("AORTS_ADMIN_TOKEN", "")
client = ControlClient(CONTROL_URL, ADMIN_TOKEN or None)

st.warning(
    "SYNTHETIC MOCK ENVIRONMENT - research prototype. Not connected to any real bank, NPCI, UPI or "
    "customer account. All accounts, amounts and identifiers are synthetic.",
    icon=":material/science:",
)


def fig_layout(fig: go.Figure, title: str, y_title: str, height: int = 300) -> go.Figure:
    fig.update_layout(
        title=title,
        height=height,
        margin=dict(l=10, r=10, t=40, b=10),
        hovermode="x unified",
        legend=dict(orientation="h", y=-0.2),
        template="plotly_white",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
    )
    fig.update_yaxes(title=y_title, gridcolor=GRID, zeroline=False)
    fig.update_xaxes(gridcolor=GRID)
    return fig


def line_series(series: dict[str, pd.Series], title: str, y_title: str) -> go.Figure:
    fig = go.Figure()
    for i, (name, s) in enumerate(series.items()):
        fig.add_trace(
            go.Scatter(
                x=s.index,
                y=s.values,
                name=name,
                mode="lines",
                line=dict(width=2, color=SERIES[i % 3]),
            )
        )
    return fig_layout(fig, title, y_title)


def grouped_bars(
    categories: list[str], groups: dict[str, list[float]], title: str, y_title: str
) -> go.Figure:
    fig = go.Figure()
    for i, (name, values) in enumerate(groups.items()):
        fig.add_trace(
            go.Bar(
                x=categories,
                y=values,
                name=name,
                marker_color=SERIES[i % 3],
                text=values,
                textposition="outside",
            )
        )
    fig.update_layout(barmode="group", bargap=0.3, bargroupgap=0.08)
    return fig_layout(fig, title, y_title)


# ------------------------------------------------------------------ sidebar: controls
with st.sidebar:
    st.header("AORTS Track B")
    st.caption(f"Control API: `{CONTROL_URL}`")
    try:
        health = client.health()
        st.success("Control service reachable", icon=":material/check_circle:")
    except Exception as exc:  # noqa: BLE001
        st.error(f"Control service unreachable: {type(exc).__name__}", icon=":material/error:")
        st.stop()
    ctrl = health.get("controller", {})
    st.caption(f"Controller: **{ctrl.get('mode')}** - reachable: {ctrl.get('reachable')}")
    auto = st.toggle("Auto-refresh every 3 s", value=False)
    lab = bool(ADMIN_TOKEN) and health.get("lab_enabled")
    faults_ok = bool(ADMIN_TOKEN) and health.get("faults_enabled")
    if not ADMIN_TOKEN:
        st.info("Read-only view: set AORTS_ADMIN_TOKEN to enable demo controls.")

    if faults_ok:
        st.subheader("Traffic & faults")
        rps = st.slider("Payment rate (per second)", 1, 50, 5)
        dur = st.slider("Duration (s)", 10, 300, 60)
        c1, c2 = st.columns(2)
        if c1.button("Start traffic", width="stretch"):
            st.toast(str(client.start_traffic({"rps": rps, "duration_s": dur})[1]))
        if c2.button("Stop traffic", width="stretch"):
            client.stop_traffic()
        c1, c2 = st.columns(2)
        if c1.button("Slow down Bank B", width="stretch"):
            client.set_fault(
                {
                    "target": "beneficiary:B",
                    "latency_ms": 1500,
                    "drop_response_rate": 0.3,
                    "note": "dashboard: Bank B slowdown",
                }
            )
        if c2.button("Restore normal", width="stretch"):
            client.reset_faults()
        st.subheader("Defenses (controlled experiments)")
        current = client.faults().get("defenses", {})
        new = {
            k: st.checkbox(k.replace("_", " "), value=v, key=f"def-{k}") for k, v in current.items()
        }
        if new != current and st.button("Apply defense settings"):
            client.set_defenses(new)
            st.rerun()

    if lab:
        st.subheader("Attack simulations (isolated)")
        rate = st.select_slider("Flood rate (req/s)", [10, 25, 50, 100], value=50)
        if st.button("Run status-check flooding (off vs on)", width="stretch"):
            st.toast(
                str(
                    client.run_attack("flooding", {"rate_per_second": rate, "duration_seconds": 5})[
                        1
                    ]
                )
            )
        if st.button("Run telemetry poisoning", width="stretch"):
            st.toast(str(client.run_attack("poisoning", {"target_route": "A1-B"})[1]))
        if st.button("Run transaction replay", width="stretch"):
            st.toast(str(client.run_attack("replay", {})[1]))

    if ADMIN_TOKEN:
        st.subheader("Escalations")
        if st.button("Reconcile escalated (HANDOFF_UDIR)", width="stretch"):
            code, body = client.reconcile_escalated()
            st.toast(f"attempted {body.get('attempted')}, resolved {body.get('resolved')}")

    if faults_ok:
        st.subheader("Data")
        if st.button("Export results (JSON to results/processed)"):
            st.toast(str(client.export_json()[1].get("saved_to")))
        st.download_button(
            "Download transactions CSV", client.export_csv(), file_name="aorts-transactions.csv"
        )
        with st.expander("Reset synthetic data"):
            st.caption(
                "Wipes and re-seeds ALL synthetic data. A snapshot is saved first. "
                "Type RESET-SYNTHETIC-DATA to confirm."
            )
            phrase = st.text_input("Confirmation phrase")
            if st.button("Reset now", disabled=phrase != "RESET-SYNTHETIC-DATA"):
                st.toast(str(client.reset(phrase)[1]))


# ------------------------------------------------------------------ data
summary = client.summary()
ts = client.timeseries(180)
pay = summary["payments"]
sec = summary["security"]
checks = summary["status_checks"]

tab_over, tab_routes, tab_twin, tab_sec, tab_val, tab_exp, tab_cmp, tab_audit = st.tabs(
    [
        "Overview",
        "Routes & recovery",
        "Twins & AORTS",
        "Security",
        "Security validation",
        "Experiments",
        "Strategy comparison",
        "Audit trail",
    ]
)

# ------------------------------------------------------------------ overview
with tab_over:
    st.caption(f"Measured at {summary['generated_at']} - {summary['label']}")
    k = st.columns(6)
    k[0].metric("Total payments", pay["total"])
    k[1].metric("Successful", pay["successful"])
    k[2].metric("Failed / reversed", pay["failed_or_reversed"])
    k[3].metric("Ambiguous backlog", pay["ambiguous_backlog"])
    k[4].metric("Escalated (HANDOFF_UDIR)", pay["escalated"])
    lat = summary["resolution_latency_s"]
    k[5].metric(
        "Resolution p50 / p95 (s)",
        f"{lat['p50'] or '-'} / {lat['p95'] or '-'}",
        help=f"{lat['samples']} ambiguous transactions resolved",
    )
    kp = summary.get("kpis", {})
    k = st.columns(4)
    k[0].metric(
        "Deadline compliance (simulated T+1)",
        "-" if kp.get("deadline_compliance") is None else f"{kp['deadline_compliance']:.1%}",
        help=f"population: {kp.get('deadline_population')} ambiguous transactions",
    )
    k[1].metric("Payment goodput (last 60 s, <=2 s)", f"{kp.get('payment_goodput_per_s_60s', 0)}/s")
    k[2].metric("Outcomes per status check", kp.get("outcomes_per_status_check") or "-")
    k[3].metric(
        "Switch timeout rate",
        "-" if kp.get("switch_timeout_rate") is None else f"{kp['switch_timeout_rate']:.1%}",
    )
    integ = summary["ledger_integrity"]
    if integ["ok"]:
        st.success(
            "Ledger integrity: balanced, no duplicate operations, balances reconcile",
            icon=":material/verified:",
        )
    else:
        st.error(f"Ledger integrity FAILED: {integ}", icon=":material/report:")

    def per_second(rows, key="n"):
        if not rows:
            return pd.Series(dtype=float)
        df = pd.DataFrame(rows)
        df["t"] = pd.to_datetime(df["t"], unit="s", utc=True)
        return df.groupby("t")[key].sum()

    st.plotly_chart(
        line_series(
            {
                "created": per_second(ts["payments_created"]),
                "became ambiguous": per_second(ts["became_ambiguous"]),
                "resolved": per_second(ts["resolved"]),
            },
            "Payments per second (measured)",
            "transactions / s",
        ),
        width="stretch",
    )
    series = ts.get("switch_series", [])
    if series:
        df = pd.DataFrame(series).fillna(0)
        df["t"] = pd.to_datetime(df["t"], unit="s", utc=True)
        df = df.set_index("t")

        def col_sum(prefix):
            cols = [c for c in df.columns if c == prefix or c.startswith(prefix + "{")]
            return df[cols].sum(axis=1) if cols else pd.Series(0, index=df.index)

        st.plotly_chart(
            line_series(
                {
                    "received": col_sum("check_received"),
                    "admitted": col_sum("check_admitted"),
                    "rejected": col_sum("check_rejected"),
                },
                "Status checks at the switch (measured)",
                "requests / s",
            ),
            width="stretch",
        )
    t = summary.get("traffic", {})
    if t.get("running") or t.get("last_result"):
        st.caption(f"Traffic generator running: {t.get('running')} - live: {t.get('live')}")

# ------------------------------------------------------------------ routes
with tab_routes:
    st.subheader("Per-route payments (measured)")
    st.dataframe(pd.DataFrame(pay["by_route"]), width="stretch", hide_index=True)
    routes = summary["switch"].get("routes") or {}
    if routes:
        st.subheader("Switch-side route measurements (independent of telemetry)")
        st.dataframe(pd.DataFrame(routes).T, width="stretch")
    st.subheader("Twin belief (predicted) vs switch measurement (observed)")
    rows = []
    for key, val in summary.get("twin", {}).items():
        if key.startswith("route:"):
            rid = key.split(":", 1)[1]
            obs = routes.get(rid.split("-", 1)[1], {})
            rows.append(
                {
                    "route": rid,
                    "twin throughput (predicted)": val.get("throughput_tps"),
                    "measured throughput": obs.get("throughput_tps"),
                    "twin p95 ms (predicted)": val.get("latency_p95_ms"),
                    "measured p95 ms": obs.get("latency_p95_ms"),
                    "integrity": summary["twin"].get(f"integrity:{rid}", {}).get("verdict"),
                }
            )
    if rows:
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    else:
        st.caption("No twin state yet - start traffic so the probe has measurements to report.")
    st.subheader("Recovery decisions (controller recommendation -> guard-applied budget)")
    decisions = client.decisions(30)
    if decisions:
        flat = []
        for d in decisions:
            for rid, b in (d.get("budgets") or {}).items():
                flat.append(
                    {
                        "time": d.get("timestamp"),
                        "policy": d.get("policy"),
                        "route": rid,
                        "recommended": b["recommended"],
                        "applied": b["applied"],
                        "integrity": b["integrity"],
                        "fallback": d.get("fallback"),
                        "reason": "; ".join(b["reasons"]) or d.get("fallback_reason") or "",
                        "dispatched": d.get("dispatched"),
                    }
                )
        st.dataframe(pd.DataFrame(flat), width="stretch", hide_index=True)
    else:
        st.caption("No recovery decisions yet (no ambiguous transactions).")

# ------------------------------------------------------------------ security
with tab_sec:
    k = st.columns(5)
    k[0].metric("Checks admitted", checks["admitted"])
    k[1].metric("Checks rejected", checks["rejected"])
    k[2].metric("Auth failures", sec["auth_failures"])
    k[3].metric("Duplicate events rejected", sec["duplicate_events_rejected"])
    k[4].metric(
        "Duplicate financial ops prevented", sec["duplicate_financial_operations_prevented"]
    )
    k = st.columns(4)
    k[0].metric(
        "Invalid / stale transitions prevented", sec["invalid_transitions_or_stale_prevented"]
    )
    tel = sec["telemetry"]
    k[1].metric("Telemetry trusted", tel.get("TRUSTED", 0))
    k[2].metric("Telemetry suspicious", tel.get("SUSPICIOUS", 0))
    k[3].metric("Telemetry rejected", tel.get("REJECTED", 0))
    banks = ["A1", "A2", "A3"]
    st.plotly_chart(
        grouped_bars(
            banks,
            {
                "admitted": [checks["admitted_by_bank"].get(b, 0) for b in banks],
                "rejected": [checks["rejected_by_bank"].get(b, 0) for b in banks],
            },
            "Status checks by remitter bank (A3 = simulated compromised bank)",
            "requests",
        ),
        width="stretch",
    )
    if checks["rejected_by_reason"]:
        st.caption(
            "Rejections by reason: "
            + ", ".join(f"{k}={v}" for k, v in checks["rejected_by_reason"].items())
        )
    st.subheader("Per-bank quotas and Tier 2 credits")
    st.dataframe(
        pd.DataFrame(
            [
                {"bank": b, **{k: v for k, v in q.items() if k != "defenses"}}
                for b, q in summary["quotas"].items()
                if isinstance(q, dict)
            ]
        ),
        width="stretch",
        hide_index=True,
    )
    st.subheader("Recent telemetry verdicts")
    if summary["telemetry_recent"]:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "at": t["at"],
                        "route": t["route_id"],
                        "verdict": t["verdict"],
                        "confidence": t["confidence"],
                        "reasons": "; ".join(t["reasons"]),
                    }
                    for t in reversed(summary["telemetry_recent"])
                ]
            ),
            width="stretch",
            hide_index=True,
        )
    st.subheader("Security events (audit, newest first)")
    events = client.security_events(100)
    if events:
        st.dataframe(
            pd.DataFrame(events)[
                ["ts", "actor", "action", "decision", "reason", "transaction_id", "route_id"]
            ],
            width="stretch",
            hide_index=True,
        )

# ------------------------------------------------------------------ security validation evidence
with tab_val:
    st.caption(
        "Recorded results of the security validation package (seeded, synthetic, local). Read from "
        f"`{ev.EVIDENCE_DIR}`; regenerate with `python -m backend.aorts.experiments.security_package`."
    )
    man = ev.manifest()
    if man.empty:
        st.info("No evidence package found. Run the security_package experiments first.")
    else:
        done = man[man["status"] == "completed"]
        k = st.columns(4)
        k[0].metric("Runs recorded", len(man))
        k[1].metric("Completed", len(done))
        k[2].metric("Failed (kept in manifest)", int((man["status"] == "failed").sum()))
        k[3].metric("Commits", man["git_commit"].nunique())

    fl = ev.runs("flooding")
    if not fl.empty:
        st.subheader("Status-check flooding (A3 = simulated attacker)")
        cond = st.selectbox(
            "Condition",
            [c for c in ev.FLOOD_ORDER if c in set(fl["condition"])],
            index=2 if "flood_defended" in set(fl["condition"]) else 0,
        )
        c1, c2 = st.columns(2)
        banks = ev.per_bank_requests(fl, cond)
        c1.plotly_chart(
            grouped_bars(
                list(banks["bank"]),
                {
                    "admitted": list(banks["admitted"].round(1)),
                    "rejected": list(banks["rejected"].round(1)),
                },
                f"Requests per bank at the switch ({cond}, mean per run)",
                "requests",
            ),
            width="stretch",
        )
        reasons = ev.rejection_reasons(fl, cond)
        c2.plotly_chart(
            grouped_bars(
                list(reasons["reason"]),
                {"A3 rejections": list(reasons["requests"].round(1))},
                "Why A3 was rejected (rate limits / Tier 2 credit exhaustion)",
                "requests",
            ),
            width="stretch",
        )
        attack_conds = [c for c in ev.FLOOD_ORDER if c != "normal" and c in set(fl["condition"])]
        means = ev.flood_by_condition(
            fl,
            [
                "legit_success_during",
                "legit_p95_ms_during",
                "detection_s",
                "containment_s",
                "recovery_after_attack_s",
                "fairness_jain_legit_banks",
                "payments_unresolved_at_end",
                "rejected_that_entered_processing",
                "attacker_share_of_admitted_during",
            ],
        )
        c1, c2 = st.columns(2)
        c1.plotly_chart(
            grouped_bars(
                list(means.index),
                {"legit p95 during attack (ms)": list(means["legit_p95_ms_during"].round(1))},
                "Legitimate A1/A2 status-check latency during the attack",
                "ms",
            ),
            width="stretch",
        )
        c2.plotly_chart(
            grouped_bars(
                list(means.index),
                {"legit success during attack": list(means["legit_success_during"].round(3))},
                "Share of legitimate checks answered during the attack",
                "share",
            ),
            width="stretch",
        )
        st.markdown("**Detection, containment, recovery and fairness (mean over seeds)**")
        st.dataframe(means.round(3), width="stretch")
        st.caption(
            "detection_s: first A3 rejection after attack start. containment_s: first second from which "
            "A3's admitted rate stays <= 1.5x its bank limit. recovery_after_attack_s: seconds after the "
            "attack until legitimate checks are healthy again. fairness: Jain index of A1/A2 admitted "
            "ratios (1 = equal). rejected_that_entered_processing must be 0: rejections happen before "
            "the costly path."
        )
        run_ids = ev.flood_run_ids(cond)
        if run_ids:
            rid = st.selectbox("Run file key (per-second timeline, backlog recovery)", run_ids)
            tl = ev.request_timeline(rid)
            if not tl.empty:
                series = {
                    f"{b} {o}": tl[(tl["bank"] == b) & (tl["outcome"] == o)].set_index("second")[
                        "requests"
                    ]
                    for b, o in (("A3", "admitted"), ("A3", "rejected"), ("A1", "admitted"))
                }
                st.plotly_chart(
                    line_series(
                        series, "Requests per second (attack window = lead..lead+attack)", "req/s"
                    ),
                    width="stretch",
                )
        with st.expander("All flooding runs (table view)"):
            st.dataframe(fl, width="stretch", hide_index=True)

    po = ev.runs("poisoning")
    if not po.empty:
        st.subheader("Telemetry poisoning (model-in-the-loop)")
        det = ev.poisoning_detection(po)
        arms = [
            a
            for a in ("poisoned_protected", "poisoned_protected_nobias", "poisoned_unprotected")
            if a in det.columns
        ]
        st.plotly_chart(
            grouped_bars(
                list(det.index),
                {a: list(det[a].round(3)) for a in arms},
                "Detection rate per poisoning case (share of attack samples not TRUSTED)",
                "share",
            ),
            width="stretch",
        )
        clean = po[po["arm"] == "clean"]
        k = st.columns(3)
        k[0].metric("False positives (clean arm, all ticks)", int(clean["flagged_all_ticks"].sum()))
        k[1].metric("Clean ticks checked", int(clean["ticks"].sum()))
        prot = po[po["arm"] == "poisoned_protected"]
        k[2].metric("Protected-arm ticks > 1 s delay", int(prot["ticks_over_1000ms"].sum()))
        with st.expander("Per case and arm (table view)"):
            st.dataframe(
                po.groupby(["case", "arm"])[
                    [
                        "detection_rate",
                        "detection_tick_offset",
                        "guard_conservative_ticks",
                        "max_budget_during_attack",
                        "ticks_over_1000ms",
                    ]
                ]
                .mean()
                .round(3),
                width="stretch",
            )
    pl = ev.runs("poisoning_live")
    if not pl.empty:
        st.subheader("Telemetry poisoning through the live ingestion path")
        det = pl.groupby(["case", "arm"])["detection_rate"].mean().unstack("arm")
        st.plotly_chart(
            grouped_bars(
                list(det.index),
                {a: list(det[a].round(3)) for a in det.columns},
                "Suspicious / rejected share of injected samples (real validator, real switch measurements)",
                "share",
            ),
            width="stretch",
        )
        with st.expander("Live poisoning runs (table view)"):
            st.dataframe(pl, width="stretch", hide_index=True)

    rp = ev.runs("replay")
    if not rp.empty:
        st.subheader("Transaction replay")
        k = st.columns(5)
        k[0].metric("Replay cases run", int(rp["cases"].sum()))
        k[1].metric("HTTP outcome as specified", int(rp["http_as_expected"].sum()))
        k[2].metric("Unexpected financial changes", int(rp["unexpected_financial_changes"].sum()))
        k[3].metric("Duplicate reversal postings", int(rp["duplicate_reversal_postings"].sum()))
        k[4].metric(
            "Ledger + audit chain OK",
            f"{int((rp['ledger_ok'] & rp['audit_chain_ok']).sum())}/{len(rp)}",
        )
        st.dataframe(rp, width="stretch", hide_index=True)

    if not man.empty:
        with st.expander("Run manifest (every run, including failures)"):
            st.dataframe(man, width="stretch", hide_index=True)

# ------------------------------------------------------------------ experiments
with tab_exp:
    if not ADMIN_TOKEN:
        st.info("Experiments need the admin token.")
    else:
        exps = client.experiments()
        if not exps:
            st.caption(
                "No experiments yet. Use the sidebar buttons or the scripts in aorts/experiments/."
            )
        else:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            k: e[k]
                            for k in (
                                "experiment_id",
                                "scenario",
                                "status",
                                "started_at",
                                "ended_at",
                            )
                        }
                        for e in exps
                    ]
                ),
                width="stretch",
                hide_index=True,
            )
            choice = st.selectbox("Inspect experiment", [e["experiment_id"] for e in exps])
            exp = client.experiment(choice)
            res = exp.get("results", {})
            st.caption(f"Raw results file: `{res.get('result_file')}`")
            if exp["scenario"] == "flooding" and "arms" in res:
                arms = res["arms"]
                names = list(arms)
                st.plotly_chart(
                    grouped_bars(
                        banks,
                        {
                            arm: [
                                arms[arm]["summary"]["per_bank"].get(b, {}).get("admitted_ok", 0)
                                for b in banks
                            ]
                            for arm in names
                        },
                        "Admitted status checks per bank: defenses off vs on (measured)",
                        "admitted requests",
                    ),
                    width="stretch",
                )
                st.dataframe(
                    pd.DataFrame(
                        {
                            arm: {k: v for k, v in arms[arm]["summary"].items() if k != "per_bank"}
                            for arm in names
                        }
                    ),
                    width="stretch",
                )
            elif exp["scenario"] == "poisoning" and "model_in_the_loop" in res:
                rows = [v["summary"] for v in res["model_in_the_loop"].values()]
                df = pd.DataFrame(rows)
                st.caption(
                    "Model-in-the-loop SIMULATION: fluid-queue route model is the ground truth."
                )
                st.dataframe(df, width="stretch", hide_index=True)
                piv = df.pivot(index="case", columns="arm", values="mean_actual_delay_ms")
                st.plotly_chart(
                    grouped_bars(
                        list(piv.index),
                        {c: list(piv[c]) for c in piv.columns},
                        "Mean actual route delay during attack (simulated)",
                        "ms",
                    ),
                    width="stretch",
                )
            elif exp["scenario"] == "replay" and "cases" in res:
                st.dataframe(pd.DataFrame(res["cases"]), width="stretch", hide_index=True)
                st.write("Ledger verification:", res.get("ledger_verification"))
            else:
                st.json(res)

# ------------------------------------------------------------------ twins + AORTS
with tab_twin:
    try:
        tw = client.twins(100)
    except Exception as exc:  # noqa: BLE001
        tw = None
        st.error(f"twin endpoint unavailable: {type(exc).__name__}")
    if tw:
        st.caption(
            tw["label"]
            + " - Route Twin capacity values are ESTIMATES; switch measurements are OBSERVED."
        )
        twins = tw["twins"]
        k = st.columns(4)
        k[0].metric("Transactions tracked", twins["transactions_tracked"])
        k[1].metric("Unresolved (twin view)", twins["unresolved_count"])
        es = twins["event_stats"]
        k[2].metric("Duplicate / stale events ignored", f"{es['duplicates']} / {es['stale']}")
        k[3].metric("Out-of-order gaps / conflicts", f"{es['gaps']} / {es['conflicts']}")
        st.subheader("Route Twins")
        last_dec = (tw.get("aorts") or {}).get("last_decision") or {}
        aorts_routes = (last_dec.get("predicted") or {}).get("routes", {})
        rows = []
        for rid, r in twins["routes"].items():
            a = aorts_routes.get(rid, {})
            est, dec = a.get("estimate", {}), a.get("decision", {})
            rows.append(
                {
                    "route": rid,
                    "primary load (rps)": r["primary_load_tps"],
                    "recovery backlog": r["recovery_backlog"],
                    "observed p50 ms": (r["measured"] or {}).get("latency_p50_ms"),
                    "telemetry reliability": r["telemetry_reliability"],
                    "last verdict": r["last_verdict"],
                    "est. capacity point / low (rps)": f"{est.get('point_rps')} / {est.get('low_rps')}"
                    if est
                    else "-",
                    "confidence": est.get("confidence"),
                    "safe budget": dec.get("budget"),
                    "mode": dec.get("mode"),
                }
            )
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
        if aorts_routes:
            rid = st.selectbox("Route decision detail", list(aorts_routes))
            dec = aorts_routes[rid]["decision"]
            st.markdown("**Budget decision reasons:** " + " · ".join(dec.get("reasons", [])))
            cands = pd.DataFrame(dec.get("candidates", []))
            if not cands.empty:
                fig = go.Figure()
                fig.add_trace(
                    go.Bar(
                        x=cands["budget"].astype(str),
                        y=cands["pred_primary_latency_ms"],
                        marker_color=[SERIES[0] if f else "#9a9a96" for f in cands["feasible"]],
                        name="predicted primary latency",
                        hovertemplate="budget %{x}: %{y} ms<extra></extra>",
                    )
                )
                st.plotly_chart(
                    fig_layout(
                        fig,
                        "Evaluated candidate budgets (blue = feasible, grey = rejected)",
                        "predicted primary latency (ms)",
                    ),
                    width="stretch",
                )
        else:
            st.info(
                "The AORTS controller is not the active strategy, so there is no budget decision to show. "
                "Select it with AORTS_CONTROLLER_MODE=aorts or POST /api/v1/admin/controller."
            )
        sel = (last_dec.get("predicted") or {}).get("selection", [])
        if sel:
            st.subheader("Selected transactions and why")
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "transaction": x["transaction_id"],
                            "route": x["route_id"],
                            "selected": x["selected"],
                            "reason": x["reason"],
                            "score": x["score"],
                            **{f"c:{k}": v for k, v in x["components"].items()},
                        }
                        for x in sel
                    ]
                ),
                width="stretch",
                hide_index=True,
            )
        pvo = (tw.get("aorts") or {}).get("predicted_vs_observed") or []
        if pvo:
            df = pd.DataFrame(pvo)
            df["at"] = pd.to_datetime(df["at"])
            fig = go.Figure()
            fig.add_trace(
                go.Scatter(
                    x=df["at"],
                    y=df["pred_primary_latency_ms"],
                    name="predicted",
                    mode="lines",
                    line=dict(width=2, color=SERIES[0]),
                )
            )
            fig.add_trace(
                go.Scatter(
                    x=df["at"],
                    y=df["observed_latency_p50_ms"],
                    name="observed (switch p50)",
                    mode="lines",
                    line=dict(width=2, color=SERIES[1]),
                )
            )
            st.plotly_chart(
                fig_layout(
                    fig, "Primary-payment latency: AORTS prediction vs next observation", "ms"
                ),
                width="stretch",
            )
        st.subheader("Transaction Twins (unresolved)")
        if twins["transactions_unresolved"]:
            st.dataframe(
                pd.DataFrame(twins["transactions_unresolved"])[
                    [
                        "transaction_id",
                        "route_id",
                        "state",
                        "version",
                        "checks_started",
                        "inconclusive_checks",
                        "ambiguous_since",
                        "deadline_at",
                        "events_applied",
                    ]
                ],
                width="stretch",
                hide_index=True,
            )
        if tw.get("dispatcher"):
            d = tw["dispatcher"]
            st.caption(
                f"Pre-dispatch gate decisions: {d.get('gate')} - duplicate jobs injected: "
                f"{d.get('duplicate_jobs_injected')}, stale jobs dropped: {d.get('stale_jobs_dropped')}"
            )

# ------------------------------------------------------------------ strategy comparison
with tab_cmp:
    summ = client.strategy_summary()
    if not summ:
        st.info(
            "No analysed strategy comparison yet. Run backend.aorts.experiments.run_strategy_comparison and "
            "backend.aorts.experiments.analyze_strategy_comparison."
        )
    else:
        st.caption(
            f"MEASURED on the Docker stack - inputs: {len(summ['inputs'])} file(s), failed runs excluded: "
            f"{summ['failed_runs']}. Intervals are bootstrap 95% CIs."
        )
        scn = st.selectbox("Scenario", list(summ["analysis"]))
        a = summ["analysis"][scn]
        st.caption(
            f"{a['seeds']} seeds x {len(a['strategies'])} strategies = {a['runs']} completed runs"
            + (" - HELD-OUT scenario (not used during development)" if a["held_out"] else "")
        )
        metric = st.selectbox("Metric", list(next(iter(a["stats"].values())).keys()))
        strat = a["strategies"]
        means = [a["stats"][s][metric].get("mean") for s in strat]
        cis = [a["stats"][s][metric].get("ci95") for s in strat]
        up = [(c[1] - m) if c and m is not None else 0 for c, m in zip(cis, means, strict=False)]
        down = [(m - c[0]) if c and m is not None else 0 for c, m in zip(cis, means, strict=False)]
        fig = go.Figure(
            go.Bar(
                x=strat,
                y=means,
                marker_color=[SERIES[1] if s == "aorts" else SERIES[0] for s in strat],
                error_y=dict(type="data", symmetric=False, array=up, arrayminus=down),
                hovertemplate="%{x}: %{y}<extra></extra>",
            )
        )
        st.plotly_chart(
            fig_layout(fig, f"{metric}: mean with 95% CI (orange = AORTS)", metric), width="stretch"
        )
        st.subheader("AORTS vs strongest baseline (paired by seed)")
        st.dataframe(
            pd.DataFrame([{"metric": m, **c} for m, c in a["aorts_vs_strongest_baseline"].items()]),
            width="stretch",
            hide_index=True,
        )

# ------------------------------------------------------------------ audit
with tab_audit:
    if not ADMIN_TOKEN:
        st.info("The audit trail needs the admin token.")
    else:
        if st.button("Verify hash chain"):
            v = client.verify_audit()
            (st.success if v["ok"] else st.error)(f"Hash chain: {v}")
        st.dataframe(pd.DataFrame(client.audit(200)), width="stretch", hide_index=True)

st.caption(f"Rendered {datetime.now(UTC):%Y-%m-%d %H:%M:%S} UTC")
if auto:
    import time

    time.sleep(3)
    st.rerun()
