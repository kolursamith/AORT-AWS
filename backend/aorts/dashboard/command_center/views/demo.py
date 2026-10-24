"""PAGE 10: Demo control center (live actions only through existing authenticated APIs)."""

from __future__ import annotations

import streamlit as st

from backend.aorts.dashboard.command_center import actions
from backend.aorts.dashboard.command_center import components as ui
from backend.aorts.dashboard.command_center import sources as src

# name: (kind, explanation, prerequisites, where to look, evidence)
DEMOS = {
    "Normal payment": ("live", "A signed payment A1 → B through the switch; the beneficiary "
                       "commits and the confirmation arrives in time.", "stack running",
                       "Live transactions", "the new row and its evidence"),
    "Lost-confirmation ambiguous payment": ("live", "The beneficiary commits but the switch "
        "response is lost (remitter lab simulation): the remitter debits and records UNKNOWN. "
        "AORTS decides whether a status inquiry is safe; Track B re-gates it.",
        "stack running; for a positive budget, honest traffic first (warm-up profile)",
        "Transaction twin · Intelligence engine", "ledger history + decision audit"),
    "Honest traffic (warm-up profile)": ("live", "12 payments/s per route for 40 s, then 3/s for "
        "120 s: demonstrated capacity above the current load, as in the Stage 10/11 protocol.",
        "stack running", "Route twin · Overview", "telemetry windows"),
    "Congested route": ("live", "Fault injection through the control API: +600 ms at "
        "beneficiary B (runtime config shared by all services).", "stack running",
        "Route twin · Intelligence engine", "control audit FAULT_INJECTION_STARTED"),
    "Route recovery": ("live", "Remove every injected fault (control API).", "a fault active",
                       "Route twin", "control audit FAULT_INJECTION_STOPPED"),
    "Flooding attack": ("recorded", "Bank A3 floods status checks; defences off vs on.",
                        "—", "Security operations · A", "Stage 10 flooding, 55 runs"),
    "Telemetry poisoning": ("recorded", "A lying telemetry source during an unannounced "
                            "capacity drop.", "—", "Security operations · B",
                            "Stage 10 poisoning, 95 runs"),
    "Transaction replay": ("recorded", "Duplicate, stale, forged and foreign financial "
                           "messages.", "—", "Security operations · C", "Stage 10 replay, 10 runs"),
    "Controller unavailable": ("recorded", "Controller down / 500 / timeout / malformed / wrong "
                               "protocol / wrong route, then latch, restart and recovery.", "—",
                               "docs/integration/STAGE10_SECURITY_INTEGRATION.md §F",
                               "Stage 10 resilience, 5 runs"),
    "Healthy parallel route": ("recorded", "Legitimate traffic on route C while A3 floods B.",
                               "—", "Security operations · A (parallel route conditions)",
                               "Stage 10 flooding"),
}  # fmt: skip


def render() -> None:
    ui.hero("DEMO CONTROL CENTER", "Approved synthetic demonstrations",
            "Live demos call the existing authenticated backend APIs (remitter, control). The UI "
            "never writes the database. Recorded demos open genuine saved runs; nothing is "
            "re-enacted.", ["LIVE", "RECORDED"])  # fmt: skip
    name = st.radio("Demonstration", list(DEMOS), horizontal=False, label_visibility="collapsed")
    kind, what, pre, where, evidence = DEMOS[name]
    left, right = st.columns([3, 2])
    with left:
        st.markdown(f"### {name} &nbsp; {ui.pill('LIVE' if kind == 'live' else 'RECORDED')}",
                    unsafe_allow_html=True)  # fmt: skip
        st.markdown(what)
        st.caption(f"Prerequisites: {pre} · where to look: {where} · evidence: {evidence}")
        if kind == "recorded":
            st.info(
                "View recorded demo: open the page named above. The runs were executed on "
                "the integrated system with real PostgreSQL; this page does not re-run them."
            )
            return
        result = None
        if name == "Normal payment":
            dest = st.selectbox("Beneficiary", ["B", "C"])
            if st.button("Run: send one payment"):
                result = actions.pay(dest, lost=False, tag="ui-normal")
        elif name == "Lost-confirmation ambiguous payment":
            n = st.slider("How many", 1, 30, 5)
            if st.button("Run: create lost-confirmation payments"):
                result = [actions.pay("B", lost=True, tag="ui-lost") for _ in range(n)]
        elif name == "Honest traffic (warm-up profile)":
            c1, c2 = st.columns(2)
            if c1.button("Start traffic"):
                result = actions.start_traffic([(12.0, 40.0), (3.0, 120.0)])
            if c2.button("Stop traffic"):
                actions.stop_traffic()
                result = "stop requested"
        elif name == "Congested route":
            if st.button("Run: add +600 ms at beneficiary B"):
                result = actions.congest()
        elif name == "Route recovery":
            if st.button("Run: remove all faults"):
                result = actions.recover()
        if result is not None:
            ui.section("Backend response", "exactly as returned")
            st.json(result if not isinstance(result, str) else {"status": result})
            st.cache_data.clear()
    with right:
        ui.section("Live status", "")
        t = actions.traffic_status()
        st.markdown(f"Honest traffic: {ui.pill('LIVE' if t['running'] else 'NO_DATA', 'running' if t['running'] else 'idle')}"
                    f"<br/><span style='font-size:12px'>{t['plan'] or ''} · sent {t['sent']} · "
                    f"errors {t['errors']}</span>", unsafe_allow_html=True)  # fmt: skip
        if st.button("Refresh status"):
            st.cache_data.clear()
        faults = None
        try:
            faults = src.control_client().faults()
        except Exception as exc:  # noqa: BLE001
            st.caption(f"faults: unavailable ({type(exc).__name__})")
        if faults is not None:
            st.caption("Active faults (control API)")
            st.json(faults.get("faults"), expanded=False)
        amb = src.scalar("SELECT count(*) FROM transactions WHERE experiment_id = :e AND "
                         "state IN ('AMBIGUOUS', 'CHECK_IN_FLIGHT')", e=src.EXP)  # fmt: skip
        st.metric("Ambiguous now (ledger)", "—" if amb is None else int(amb))
        view, err = src.controller()
        if view:
            for route in ("A1-B", "A1-C"):
                d = (view["routes"].get(route) or {}).get("last_decision") or {}
                st.markdown(f"**{route}** {ui.pill(d.get('decision_mode') or 'NO_DATA')} "
                            f"<code>{d.get('reason_code', '—')}</code> · {d.get('budget_rps', '—')}/s",
                            unsafe_allow_html=True)  # fmt: skip
        else:
            st.caption(f"controller: {err}")
        ui.note("A positive budget needs demonstrated capacity above the current load and a "
                "backlog worth checking. Lost confirmations that arrive <i>during</i> honest "
                "windows look like congestion to the estimator, and it stays at zero; that is "
                "the documented Stage 11 finding, not a fault.", "warn")  # fmt: skip
