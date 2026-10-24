"""PAGE 8: Financial ledger and audit (read-only role; no write path exists here)."""

from __future__ import annotations

import streamlit as st

from backend.aorts.dashboard.command_center import components as ui
from backend.aorts.dashboard.command_center import sources as src


def render() -> None:
    ui.hero("FINANCIAL LEDGER & AUDIT", "Authoritative money movement and its evidence",
            "Read through the aorts_dashboard role: SELECT-only grants and read-only "
            "transactions. Keys, tokens and signatures are never displayed.", ["LIVE"])  # fmt: skip
    summary, serr = src.v11_summary()
    safety = (summary or {}).get("financial_safety") or {}
    balance, berr = src.query(
        """SELECT sum(CASE WHEN direction = 'DEBIT' THEN amount_paise ELSE 0 END) AS debits,
                  sum(CASE WHEN direction = 'CREDIT' THEN amount_paise ELSE 0 END) AS credits,
                  count(*) AS entries FROM ledger_entries"""
    )
    dup_ops = ui_scalar("""SELECT count(*) FROM (SELECT transaction_id, operation_type FROM
                           financial_operations WHERE operation_type IN ('DEBIT', 'REVERSAL')
                           GROUP BY 1, 2 HAVING count(*) > 1) d""")  # fmt: skip
    dup_keys = ui_scalar("""SELECT count(*) FROM (SELECT idempotency_key FROM transactions
                            GROUP BY 1 HAVING count(*) > 1) d""")  # fmt: skip
    if berr:
        ui.empty("ledger entries", berr)
    else:
        b = balance.iloc[0]
        balanced = b.debits == b.credits
        ui.kpi_row([
            ui.kpi("Ledger integrity", "OK" if safety.get("ledger_ok") else ("FAILED" if safety else None),
                   "control API verify_ledger()" if safety else serr or "",
                   "emerald" if safety.get("ledger_ok") else "red"),
            ui.kpi("Double entry", "balanced" if balanced else "IMBALANCED",
                   f"{int(b.entries):,} entries", "emerald" if balanced else "red"),
            ui.kpi("Second debit/reversal", dup_ops, "per transaction (must be 0)",
                   "emerald" if dup_ops == 0 else "red"),
            ui.kpi("Duplicate idempotency keys", dup_keys, "unique constraint (must be 0)",
                   "emerald" if dup_keys == 0 else "red"),
            ui.kpi("Unverified evidence applied", safety.get("unverified_evidence_applied"),
                   "ledger check", "emerald" if safety.get("unverified_evidence_applied") == 0 else ""),
        ])  # fmt: skip
    c1, c2 = st.columns(2)
    with c1:
        ui.section("Financial operations", "by type and result")
        ops, err = src.query("""SELECT operation_type, result, count(*) AS n,
                                round(sum(amount_paise) / 100.0, 2) AS amount_inr
                                FROM financial_operations GROUP BY 1, 2 ORDER BY 1, 2""")  # fmt: skip
        st.dataframe(ops, hide_index=True, use_container_width=True) if not err else ui.empty(
            "operations", err
        )
        ui.section("Event outbox", "at-least-once delivery state")
        ob, err = src.query("""SELECT event_type, count(*) AS events,
                               count(*) FILTER (WHERE dead_lettered) AS dead_lettered
                               FROM outbox_events GROUP BY 1 ORDER BY 2 DESC""")  # fmt: skip
        st.dataframe(ob, hide_index=True, use_container_width=True) if not err else ui.empty(
            "outbox", err
        )
        cur, _ = src.query(
            "SELECT stream, last_id, attempts, last_error, updated_at FROM v11_delivery_state"
        )
        if cur is not None and not cur.empty:
            st.caption("v1.1 delivery cursors")
            st.dataframe(cur, hide_index=True, use_container_width=True)
    with c2:
        ui.section("Evidence records", "signed responses; only verified evidence can settle")
        ev, err = src.query("""SELECT evidence_kind, reported_status, verified, count(*) AS n
                               FROM evidence_records GROUP BY 1, 2, 3 ORDER BY 1, 2, 3""")  # fmt: skip
        st.dataframe(ev, hide_index=True, use_container_width=True) if not err else ui.empty(
            "evidence", err
        )
        ui.section("Refusals and rejections", "audit log, by action")
        rej, err = src.query("""SELECT action, decision, count(*) AS n FROM audit_events
                                WHERE decision IN ('REJECTED', 'FALLBACK', 'DEFERRED')
                                GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 20""")  # fmt: skip
        st.dataframe(rej, hide_index=True, use_container_width=True) if not err else ui.empty(
            "audit", err
        )
    ui.section("Audit trail", "hash-chained audit_events (newest 60)")
    au, err = src.query("""SELECT seq, ts, actor, action, decision, transaction_id, route_id,
                           correlation_id, left(reason, 80) AS reason, left(hash, 12) AS hash
                           FROM audit_events ORDER BY seq DESC LIMIT 60""")  # fmt: skip
    if err:
        ui.empty("audit trail", err)
    else:
        st.dataframe(au, hide_index=True, use_container_width=True, height=360)
    ui.source_line(
        "LIVE: PostgreSQL (aorts_dashboard, read-only)", "control /api/v1/aorts/v11/summary"
    )


def ui_scalar(sql: str):
    v = src.scalar(sql)
    return None if v is None else int(v)
