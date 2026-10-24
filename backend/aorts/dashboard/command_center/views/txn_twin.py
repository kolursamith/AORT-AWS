"""PAGE 4: Transaction Digital Twin (controller projection beside the authoritative ledger)."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from backend.aorts.dashboard.command_center import components as ui
from backend.aorts.dashboard.command_center import sources as src
from backend.aorts.dashboard.command_center.views.transactions import detail

LIFECYCLE = [
    ("INITIATED", "PAYMENT_INITIATED / DEBITED"),
    ("AMBIGUOUS", "client timeout, outcome UNKNOWN"),
    ("STATUS_CHECK_ELIGIBLE", "timing policy permits a check"),
    ("CHECK_IN_FLIGHT", "status inquiry dispatched"),
    ("AUTHORITATIVE_RESULT", "verified signed answer"),
    ("RESOLVED", "terminal ledger state"),
]


def _reached(p: dict, ledger_events: list[str]) -> list[bool]:
    ambiguous = p.get("ambiguity_created_at") is not None or "PAYMENT_AMBIGUOUS" in ledger_events
    checked = p.get("checks_used", 0) > 0 or "CHECK_STARTED" in ledger_events
    authoritative = p.get("last_authoritative_evidence_at") is not None or any(
        e in ledger_events for e in ("PAYMENT_SUCCEEDED", "PAYMENT_FAILED")
    )
    eligible = ambiguous and (checked or p.get("next_eligible_at") is not None)
    return [True, ambiguous, eligible, checked, authoritative, bool(p.get("terminal"))]


def render() -> None:
    ui.hero("TRANSACTION DIGITAL TWIN", "A non-authoritative projection of committed events",
            "The twin follows ledger facts delivered over v1.1 and derives the recovery workflow "
            "and check eligibility. It never moves money; the ledger stays authoritative.",
            ["LIVE"])  # fmt: skip
    view, err = src.controller()
    if err:
        ui.empty("transaction twin projections", err)
        return
    projections = view.get("transaction_projections") or []
    if not projections:
        ui.no_data("The controller holds no transaction projections yet",
                   "Create a lost-confirmation payment on the Demo control page")  # fmt: skip
        return
    df = pd.DataFrame(projections)
    show = ["transaction_id", "route_id", "workflow_state", "ledger_state_observed",
            "financial_outcome", "version", "checks_used", "next_eligible_at", "deadline_at",
            "terminal"]  # fmt: skip
    only_amb = st.toggle("Only payments that became ambiguous", value=True)
    if only_amb:
        df = df[df.ambiguity_created_at.notna()]
    ui.section("Projections", f"{len(df)} of {len(projections)} newest held by the controller")
    if df.empty:
        ui.no_data("No ambiguous payments in the projection window", "")
        return
    st.dataframe(df[show].iloc[::-1], hide_index=True, use_container_width=True, height=280)
    pick = st.selectbox("Projection", df.transaction_id.iloc[::-1].tolist())
    p = next(x for x in projections if x["transaction_id"] == pick)
    ev, _ = src.query("SELECT event_type FROM transaction_events WHERE transaction_id = :t",
                      t=pick)  # fmt: skip
    events = ev.event_type.tolist() if ev is not None else []
    reached = _reached(p, events)
    steps = [(name, why, "ok" if hit else "") for (name, why), hit in
             zip(LIFECYCLE, reached, strict=True)]  # fmt: skip
    ui.section("Recovery lifecycle", "derived from the twin and the immutable ledger history")
    ui.pipeline(steps)
    ui.kpi_row([
        ui.kpi("Twin version", p.get("version"), "backend state_version", "cyan"),
        ui.kpi("Checks used", p.get("checks_used"), "counted attempts"),
        ui.kpi("Workflow", p.get("workflow_state"), "twin-derived"),
        ui.kpi("Outcome", p.get("financial_outcome"), "copied from ledger events",
               "emerald" if p.get("financial_outcome") == "SUCCESS" else "amber"),
        ui.kpi("Deadline", (p.get("deadline_at") or "—")[:19], "policy deadline"),
    ])  # fmt: skip
    with st.expander("Projection (raw, as returned by the controller)"):
        st.json(p)
    rec = [r for r in view.get("decision_records") or [] if r.get("route_id") == p.get("route_id")]
    if rec:
        last = rec[-1]
        ui.note(f"Last controller decision on {p.get('route_id')}: <b>{last.get('mode')}</b> · "
                f"<code>{last.get('reason')}</code> · budget {last.get('budget_rps')}/s · "
                f"selected {last.get('selected')} · {ui.when(last.get('created_at'))}")  # fmt: skip
    detail(pick)
