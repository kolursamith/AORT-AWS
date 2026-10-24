"""PAGE 5: AORTS Intelligence Engine: the decision pipeline, explained from live values."""

from __future__ import annotations

import json

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from backend.aorts.dashboard.command_center import components as ui
from backend.aorts.dashboard.command_center import sources as src
from backend.aorts.dashboard.command_center.theme import INK

WHY = {
    "SAFE_BUDGET_SELECTED": "A positive budget beat zero checks in the pessimistic capacity band "
    "by at least the required margin, and also improved in the central band.",
    "NO_SAFE_BUDGET": "Either the conservative headroom cap is 0, or no positive budget improved "
    "the objective on zero by the required margin. Zero checks is the safe choice, not an error.",
    "NO_ELIGIBLE_TRANSACTIONS": "No ambiguous payment is eligible under the timing policy now.",
    "CONTROLLER_WARMING_UP": "The estimator has not yet seen enough trusted windows.",
    "TELEMETRY_STALE": "Trusted telemetry is too old, or Track B's delivered watermark is ahead of "
    "what the controller applied: fail closed.",
    "TELEMETRY_UNTRUSTED": "Integrity problem in the telemetry (or an explicit latch): fail closed.",
    "NO_CAPACITY_ESTIMATE": "No usable capacity estimate: fail closed.",
}


def _terms(e: dict) -> dict:
    return {k: v for k, v in (e.get("terms") or [])}


def render() -> None:
    ui.hero("AORTS INTELLIGENCE ENGINE", "How the controller chooses a recovery-check budget",
            "Every value below is the controller's own latest computation. Forecasts are MODEL "
            "PREDICTIONS (Stage 07 calibration was weak); only the ledger records authoritative "
            "financial results.", ["LIVE", "MODEL PREDICTION"])  # fmt: skip
    view, err = src.controller()
    if err:
        ui.empty("controller decision state", err)
        return
    route = st.radio("Route", [r for r in view["routes"] if r in ("A1-B", "A1-C")],
                     horizontal=True)  # fmt: skip
    r = view["routes"][route]
    est, opt, dec = r.get("estimate") or {}, r.get("optimisation"), r.get("last_decision")
    snap = r.get("snapshot") or {}
    audit, _ = src.query(
        """SELECT ts, decision, metadata::text AS m FROM audit_events
           WHERE action = 'RECOVERY_DECISION_V11' AND experiment_id = :e AND route_id = :r
           ORDER BY seq DESC LIMIT 1""",
        e=src.EXP,
        r=route,
    )
    gate = json.loads(audit.iloc[0].m) if audit is not None and not audit.empty else {}
    trusted = snap.get("freshness_status") == "FRESH"
    head = est.get("safe_headroom_rps")
    budget = None if dec is None else dec.get("budget_rps")
    ui.pipeline([
        ("Trusted telemetry", f"{snap.get('freshness_status', '—')} · v{snap.get('snapshot_version', '—')}",
         "ok" if trusted else "warn"),
        ("Route Digital Twin", f"{est.get('route_state', '—')}", "ok" if est else "warn"),
        ("Capacity estimator", f"{est.get('mode', '—')} · headroom "
         f"{'—' if head is None else f'{head:.2f}/s'}",
         "ok" if head else "warn"),
        ("Feedback look-ahead", "available" if opt and opt.get("forecast") and
         opt["forecast"].get("available") else "not run", "ok" if opt and opt.get("forecast") else ""),
        ("Safe budget optimizer", f"cap {opt.get('cap_rps') if opt else '—'} → {budget if budget is not None else '—'}/s",
         "ok" if budget else "warn"),
        ("Transaction ranker", f"{len((dec or {}).get('selected_transaction_ids') or [])} selected", ""),
        ("Backend execution gate", f"{gate.get('admitted', '—')} admitted · {gate.get('dispatched', '—')} sent"
         if gate else "no tick yet", "ok" if gate.get("dispatched") else ""),
    ])  # fmt: skip

    left, right = st.columns([3, 2])
    with right:
        ui.section("Why did AORTS choose this?", "real reason codes")
        if dec is None:
            ui.no_data("No decision requested on this route yet", "the dispatcher asks every tick")
        else:
            code = dec.get("reason_code")
            st.markdown(f"{ui.pill(dec.get('decision_mode'))} &nbsp; <code>{code}</code> · "
                        f"budget **{dec.get('budget_rps')}/s**", unsafe_allow_html=True)  # fmt: skip
            ui.note(WHY.get(code, ""))
            st.markdown(f"<span style='color:{INK['muted']};font-size:12.5px'>"
                        f"{dec.get('reason_detail', '')}</span>", unsafe_allow_html=True)  # fmt: skip
            if est.get("reason_codes"):
                st.caption("Estimator reason codes: " + ", ".join(est["reason_codes"]))
            st.caption(f"Decision `{dec.get('decision_id')}` · created {ui.when(dec.get('created_at'))}"
                       f" · expires {ui.when(dec.get('expires_at'))}")  # fmt: skip
            sel = dec.get("selected_transaction_ids") or []
            if sel:
                st.caption(
                    "Selected (ranked): " + ", ".join(sel[:8]) + (" …" if len(sel) > 8 else "")
                )
        ui.section("Backend execution gate", "Track B, latest tick on this route")
        if gate:
            ui.kpi_row([
                ui.kpi("Outcome", gate.get("outcome"), f"source {gate.get('source')}"),
                ui.kpi("Admitted", gate.get("admitted"), f"of {gate.get('selected')} selected", "emerald"),
                ui.kpi("Sent", gate.get("dispatched"), "status checks"),
            ])  # fmt: skip
            refused = gate.get("gate_refused") or {}
            if refused:
                st.caption("Refused by the gate: " + ", ".join(
                    f"{t[-8:]}→{c}" for t, c in list(refused.items())[:6]))  # fmt: skip
        else:
            ui.no_data("No dispatcher tick recorded yet", "the aorts-loop writes one per tick")
    with left:
        ui.section(
            "Candidate budgets",
            "objective in transaction-equivalents (lower is better)",
            badge="MODEL PREDICTION",
        )
        if not opt:
            ui.no_data(
                "No optimisation on this route yet",
                "the optimizer runs only when the estimator is usable and something is eligible",
            )
        elif not opt.get("evaluations"):
            ui.note(f"The optimizer ran with a budget cap of <b>{opt.get('cap_rps')}</b> checks/s, "
                    "so no positive candidate was evaluated: zero checks (reasons: <code>"
                    f"{', '.join(opt.get('reasons') or []) or '—'}</code>).", "warn")  # fmt: skip
        else:
            rows = []
            for e in opt["evaluations"]:
                t = _terms(e)
                rows.append({"budget /s": e["budget_rps"], "objective (pessimistic)": e.get("objective"),
                             "objective (central)": e.get("objective_central"),
                             "new ambiguity term": t.get("new_ambiguity"), "backlog term": t.get("backlog"),
                             "delay term": t.get("delay"), "check cost": t.get("check"),
                             "resolution benefit": t.get("resolution_benefit"),
                             "binding": ", ".join(e.get("binding") or [])})  # fmt: skip
            ev = pd.DataFrame(rows)
            st.dataframe(ev, hide_index=True, use_container_width=True)
            fig = go.Figure()
            fig.add_bar(x=ev["budget /s"].astype(str), y=ev["objective (pessimistic)"],
                        name="pessimistic band", marker_color=INK["cyan"])  # fmt: skip
            fig.add_bar(x=ev["budget /s"].astype(str), y=ev["objective (central)"],
                        name="central band", marker_color=INK["violet"])  # fmt: skip
            fig.update_layout(barmode="group", height=260, xaxis_title="candidate budget (checks/s)",
                              yaxis_title="objective J(b)")  # fmt: skip
            st.plotly_chart(fig, use_container_width=True)
            st.caption(f"Profile `{opt.get('profile')}` · ablation `{opt.get('ablation')}` · cap "
                       f"{opt.get('cap_rps')}/s · selected {opt.get('selected_budget_rps')}/s · "
                       f"capacity confidence (heuristic) {opt.get('capacity_confidence_heuristic')}")  # fmt: skip
        fc = (opt or {}).get("forecast")
        if fc and fc.get("budgets"):
            ui.section("Look-ahead forecast", f"horizon {fc.get('horizon_s')} s · check benefit "
                       f"lower bound {round(fc.get('check_benefit_low') or 0, 3)}", badge="MODEL PREDICTION")  # fmt: skip
            rows = []
            for b in fc["budgets"]:
                for band in b["bands"]:
                    rows.append({"budget": b["budget_rps"], "band": band["band"],
                                 "new ambiguous": band["new_ambiguous"],
                                 "payment delay ms": band["predicted_payment_delay_ms"],
                                 "expected authoritative": band["expected_authoritative"]})  # fmt: skip
            f = pd.DataFrame(rows)
            fig = go.Figure()
            for i, band in enumerate(sorted(f.band.unique())):
                d = f[f.band == band]
                fig.add_scatter(x=d.budget, y=d["new ambiguous"], mode="lines+markers", name=band,
                                line=dict(color=[INK["red"], INK["cyan"], INK["emerald"]][i % 3]))  # fmt: skip
            fig.update_layout(height=240, xaxis_title="budget (checks/s)",
                              yaxis_title="predicted new ambiguous (horizon)")  # fmt: skip
            st.plotly_chart(fig, use_container_width=True)
    ui.section("Predicted vs observed vs authoritative", "three different kinds of number")
    c1, c2, c3 = st.columns(3)
    c1.markdown(f"{ui.pill('MODEL PREDICTION')}<br/>Forecast new ambiguity and delay above: what the "
                "look-ahead expects, not a guarantee.", unsafe_allow_html=True)  # fmt: skip
    c2.markdown(f"{ui.pill('LIVE', 'Observed')}<br/>Telemetry windows and gate outcomes: what Track B "
                "measured and enforced.", unsafe_allow_html=True)  # fmt: skip
    c3.markdown(f"{ui.pill('VERIFIED', 'Authoritative')}<br/>Ledger states with verified signed "
                "evidence: the only financial truth.", unsafe_allow_html=True)  # fmt: skip
    recs = pd.DataFrame(view.get("decision_records") or [])
    if not recs.empty:
        ui.section("Recent decisions", "controller decision log (this process)")
        recs = recs[recs.route_id == route][["created_at", "mode", "reason", "budget_rps", "selected",
                                             "source", "decision_id"]].iloc[::-1].head(25)  # fmt: skip
        st.dataframe(recs, hide_index=True, use_container_width=True, height=260)
    ui.source_line("LIVE: controller /v1.1/introspection", "dispatcher audit RECOVERY_DECISION_V11")
