"""PAGE 2: Live transaction monitor (ledger, read-only)."""

from __future__ import annotations

import json

import streamlit as st

from backend.aorts.dashboard.command_center import components as ui
from backend.aorts.dashboard.command_center import sources as src

TONE = {"CLOSED": "ok", "CONFIRMED_SUCCESS": "ok", "REVERSED": "ok", "AMBIGUOUS": "warn",
        "CHECK_IN_FLIGHT": "", "CONFIRMED_FAILED": "bad", "HANDOFF_UDIR": "bad"}  # fmt: skip


def render() -> None:
    ui.hero("LIVE TRANSACTION MONITOR", "Synthetic payment lifecycle",
            "Every row is read from the authoritative PostgreSQL ledger through a read-only "
            "role. Recovery checks, evidence and decisions are joined per payment.",
            ["LIVE"])  # fmt: skip
    with st.container():
        c1, c2, c3, c4, c5 = st.columns([2.2, 1, 1, 1.4, 1])
        needle = c1.text_input("Search transaction / correlation id", "")
        bank = c2.selectbox("Beneficiary", ["all", "B", "C"])
        route = c3.selectbox("Route", ["all", "A1-B", "A1-C"])
        states = c4.multiselect("State", ["AMBIGUOUS", "CHECK_IN_FLIGHT", "CLOSED", "REVERSED",
                                          "CONFIRMED_SUCCESS", "CONFIRMED_FAILED", "DEBITED",
                                          "HANDOFF_UDIR"])  # fmt: skip
        minutes = c5.selectbox("Window", [15, 60, 240, 1440], index=1, format_func=lambda m:
                               f"last {m} min" if m < 1440 else "last 24 h")  # fmt: skip
        if st.button("Refresh", help="re-read the ledger (cache 4 s)"):
            st.cache_data.clear()
    df, err = src.query(
        """SELECT t.transaction_id, t.remitter_bank_id AS remitter, t.beneficiary_bank_id AS beneficiary,
                  t.route_id AS route, round(t.amount_paise / 100.0, 2) AS amount_inr,
                  t.created_at, t.state, t.financial_outcome AS outcome, t.checks_used,
                  t.deadline_at, t.version,
                  (SELECT ev.evidence_kind || ' ' || coalesce(ev.reported_status, '?') ||
                          CASE WHEN ev.verified THEN ' (verified)' ELSE ' (UNVERIFIED)' END
                   FROM evidence_records ev WHERE ev.transaction_id = t.transaction_id
                   ORDER BY ev.created_at DESC LIMIT 1) AS last_evidence,
                  t.updated_at, t.correlation_id
           FROM transactions t
           WHERE t.experiment_id = :e AND t.created_at > now() - make_interval(mins => :m)
             AND (:bank = 'all' OR t.beneficiary_bank_id = :bank)
             AND (:route = 'all' OR t.route_id = :route)
             AND (:needle = '' OR t.transaction_id ILIKE '%' || :needle || '%'
                  OR t.correlation_id ILIKE '%' || :needle || '%')
           ORDER BY t.created_at DESC LIMIT 500""",
        e=src.EXP, m=int(minutes), bank=bank, route=route, needle=needle.strip(),
    )  # fmt: skip
    if err:
        ui.empty("transactions", err)
        return
    if states:
        df = df[df.state.isin(states)]
    ui.section("Payments", f"{len(df)} shown (newest first, max 500)")
    if df.empty:
        ui.no_data("No payments match", "Create one on the Demo control page")
        return
    st.dataframe(df.drop(columns=["correlation_id"]), hide_index=True, use_container_width=True,
                 height=360)  # fmt: skip
    pick = st.selectbox("Transaction detail", df.transaction_id.tolist())
    detail(pick)


def detail(txn: str) -> None:
    ev, err = src.query(
        """SELECT new_version, event_type, previous_state, new_state, actor, reason,
                  metadata::text AS metadata, created_at
           FROM transaction_events WHERE transaction_id = :t ORDER BY new_version""",
        t=txn,
    )
    evid, _ = src.query(
        """SELECT created_at, evidence_kind, reported_status, verified, key_id, check_id,
                  latency_ms, evidence_id FROM evidence_records WHERE transaction_id = :t
           ORDER BY created_at""",
        t=txn,
    )
    left, right = st.columns([3, 2])
    with left:
        ui.section("Authoritative event timeline", "transaction_events (immutable history)")
        if err or ev is None or ev.empty:
            ui.empty("event history", err or "no events")
        else:
            items = []
            for r in ev.itertuples():
                meta = json.loads(r.metadata or "{}")
                extra = ", ".join(f"{k}={v}" for k, v in meta.items()
                                  if k in ("ambiguity_cause", "check_id", "evidence_id"))  # fmt: skip
                items.append((f"v{r.new_version} · {r.event_type}",
                              f"{r.previous_state or '—'} → {r.new_state} · {r.actor} · "
                              f"{ui.when(r.created_at)}" + (f" · {extra}" if extra else ""),
                              TONE.get(r.new_state, "")))  # fmt: skip
            ui.timeline(items)
    with right:
        ui.section("Evidence", "signed responses as received; verified by HMAC")
        if evid is None or evid.empty:
            ui.no_data("No evidence records", "")
        else:
            st.dataframe(evid, hide_index=True, use_container_width=True)
        dec, _ = src.query(
            """SELECT ts, route_id, decision, reason, metadata->>'decision_id' AS decision_id,
                      metadata->>'controller_reason' AS controller_reason
               FROM audit_events WHERE action = 'RECOVERY_DECISION_V11' AND experiment_id = :e
                 AND metadata::text LIKE :t ORDER BY seq""",
            e=src.EXP,
            t=f"%{txn}%",
        )
        ui.section("Controller decisions naming it", "dispatcher audit")
        if dec is None or dec.empty:
            ui.no_data("No decision selected this payment", "zero budgets select nothing")
        else:
            st.dataframe(dec, hide_index=True, use_container_width=True)
