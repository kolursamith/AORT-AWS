"""PAGE 1: Executive overview (live)."""

from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

from backend.aorts.dashboard.command_center import components as ui
from backend.aorts.dashboard.command_center import sources as src
from backend.aorts.dashboard.command_center.theme import INK

UNRESOLVED = ("AMBIGUOUS", "CHECK_IN_FLIGHT")


def route_state(view: dict | None, route: str) -> tuple[str, str]:
    """The controller's own returned state for a route (never inferred from HTTP 200)."""

    if view is None:
        return "UNAVAILABLE", "controller unreachable"
    r = view["routes"].get(route) or {}
    est, dec = r.get("estimate"), r.get("last_decision")
    if est is None:
        return "WARMING_UP", "no capacity estimate yet"
    if dec and dec.get("decision_mode") in ("FAIL_CLOSED", "WARMING_UP"):
        return dec["decision_mode"], dec.get("reason_code", "")
    state = est.get("route_state")
    if state == "HEALTHY":
        return "HEALTHY", f"estimator {est.get('mode')}"
    return ("DEGRADED" if state in ("RECOVERING", "CONGESTED") else state or "UNAVAILABLE"), (
        f"route {state}, estimator {est.get('mode')}"
    )


def render() -> None:
    view, verr = src.controller()
    summary, serr = src.v11_summary()
    ui.hero(
        "AORTS COMMAND CENTER",
        "Adaptive Payment Recovery • Digital Twins • Cybersecurity",
        f"Live synthetic experiment `{src.EXP}`: PostgreSQL ledger, Track A controller and "
        "Track B banking services. Synthetic mock environment: no real bank or customer data.",
        ["LIVE", "HEALTHY" if view else "UNAVAILABLE"],
    )
    k, err = src.query(
        """SELECT count(*) AS total,
                  count(*) FILTER (WHERE financial_outcome = 'SUCCESS') AS success,
                  count(*) FILTER (WHERE state = ANY(:u)) AS ambiguous_now,
                  count(*) FILTER (WHERE ambiguous_since IS NOT NULL) AS ever_ambiguous,
                  count(*) FILTER (WHERE ambiguous_since IS NOT NULL
                                   AND NOT (state = ANY(:u)) AND state <> 'DEBITED') AS resolved,
                  count(*) FILTER (WHERE state = 'CHECK_IN_FLIGHT') AS in_flight
           FROM transactions WHERE experiment_id = :e""",
        e=src.EXP,
        u=list(UNRESOLVED),
    )
    if err:
        ui.empty("ledger KPIs", err)
        return
    row = k.iloc[0]
    lat, _ = src.query(
        """SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50,
                  percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95, count(*) AS n
           FROM evidence_records ev JOIN transactions t USING (transaction_id)
           WHERE t.experiment_id = :e AND ev.evidence_kind = 'ORIGINAL_PAYMENT_RESPONSE'
             AND ev.latency_ms IS NOT NULL""",
        e=src.EXP,
    )
    checks, _ = src.query(
        """SELECT count(*) FILTER (WHERE te.event_type = 'CHECK_STARTED') AS started,
                  (SELECT count(*) FROM evidence_records ev JOIN transactions t2
                     USING (transaction_id) WHERE t2.experiment_id = :e
                     AND ev.evidence_kind = 'STATUS_CHECK_RESPONSE' AND ev.verified
                     AND ev.reported_status IN ('SUCCESS', 'FAILED')) AS authoritative
           FROM transaction_events te JOIN transactions t USING (transaction_id)
           WHERE t.experiment_id = :e""",
        e=src.EXP,
    )
    untrusted = src.scalar(
        """SELECT count(*) FROM (SELECT verdict FROM v11_telemetry_windows
           WHERE experiment_id = :e ORDER BY id DESC LIMIT 200) w WHERE verdict <> 'TRUSTED'""",
        e=src.EXP,
    )
    routes_live = src.scalar(
        """SELECT count(DISTINCT route_id) FROM v11_telemetry_windows WHERE experiment_id = :e
           AND window_end > now() - interval '60 seconds'""",
        e=src.EXP,
    )
    started = int(checks.iloc[0]["started"]) if checks is not None else 0
    auth = int(checks.iloc[0]["authoritative"]) if checks is not None else 0
    goodput = f"{auth / started:.0%}" if started else None
    safety = (summary or {}).get("financial_safety") or {}
    violations = None
    if safety:
        violations = sum(int(safety.get(k) or 0) for k in
                         ("duplicate_check_starts", "multiple_reversals",
                          "unverified_evidence_applied")) + (0 if safety.get("ledger_ok") else 1)  # fmt: skip
    ui.kpi_row([
        ui.kpi("Synthetic transactions", int(row.total), "all payments in this experiment", "cyan"),
        ui.kpi("Successful", int(row.success), "authoritative SUCCESS outcome", "emerald"),
        ui.kpi("Ambiguous now", int(row.ambiguous_now), "UNKNOWN outcome, awaiting evidence",
               "amber" if row.ambiguous_now else ""),
        ui.kpi("Resolved ambiguities", int(row.resolved), f"of {int(row.ever_ambiguous)} ever "
               "ambiguous", "emerald"),
        ui.kpi("Checks in flight", int(row.in_flight), "status inquiries outstanding"),
    ])  # fmt: skip
    st.write("")
    p95 = None if lat is None or lat.iloc[0]["n"] == 0 else round(float(lat.iloc[0]["p95"]), 1)
    ui.kpi_row([
        ui.kpi("Recovery goodput", goodput, f"{auth} authoritative answers / {started} checks"),
        ui.kpi("Payment latency p95", None if p95 is None else f"{p95} ms",
               "signed original responses" if p95 is not None else "no responses yet"),
        ui.kpi("Untrusted telemetry", untrusted, "of the latest 200 windows (validator)",
               "amber" if untrusted else ""),
        ui.kpi("Active routes", routes_live, "telemetry in the last 60 s"),
        ui.kpi("Ledger safety violations", violations,
               "duplicate checks, 2nd reversals, unverified evidence, integrity" if safety
               else serr or "control API", "emerald" if violations == 0 else "red"),
    ])  # fmt: skip

    ui.section("System health", "states as returned by the services (not inferred from HTTP 200)")
    h = src.health()
    cols = st.columns(len(h) + 2)
    for col, (name, info) in zip(cols, h.items(), strict=False):
        state = "HEALTHY" if info.get("http") == 200 else "UNAVAILABLE"
        col.markdown(f"**{name}**<br/>{ui.pill(state)}", unsafe_allow_html=True)
    for col, route in zip(cols[len(h) :], ("A1-B", "A1-C"), strict=False):
        state, why = route_state(view, route)
        col.markdown(f"**route {route}**<br/>{ui.pill(state)}<br/><span style='color:"
                     f"{INK['faint']};font-size:11px'>{why}</span>", unsafe_allow_html=True)  # fmt: skip
    if verr:
        ui.note(f"Controller: {verr}", "warn")

    left, right = st.columns([3, 2])
    with left:
        ui.section("Transaction volume", "payments created per 10 s, last 30 min (ledger)")
        vol, _ = src.query(
            """SELECT to_timestamp(floor(extract(epoch FROM created_at) / 10) * 10) AS t,
                      count(*) AS n,
                      count(*) FILTER (WHERE ambiguous_since IS NOT NULL) AS ambiguous
               FROM transactions WHERE experiment_id = :e
                 AND created_at > now() - interval '30 minutes' GROUP BY 1 ORDER BY 1""",
            e=src.EXP,
        )
        if vol is None or vol.empty:
            ui.no_data("No payments in the last 30 minutes", "Run honest traffic from Demo control")
        else:
            fig = go.Figure()
            fig.add_bar(x=vol.t, y=vol.n - vol.ambiguous, name="confirmed path",
                        marker_color=INK["cyan"])  # fmt: skip
            fig.add_bar(x=vol.t, y=vol.ambiguous, name="became ambiguous",
                        marker_color=INK["amber"])  # fmt: skip
            fig.update_layout(barmode="stack", height=280)
            st.plotly_chart(fig, use_container_width=True)
    with right:
        ui.section("Outcome distribution", "current ledger state")
        dist, _ = src.query(
            "SELECT state, count(*) AS n FROM transactions WHERE experiment_id = :e GROUP BY 1",
            e=src.EXP,
        )
        if dist is None or dist.empty:
            ui.no_data("No transactions", "Create a payment from Demo control")
        else:
            colours = {"CLOSED": INK["emerald"], "AMBIGUOUS": INK["amber"],
                       "CHECK_IN_FLIGHT": INK["blue"], "REVERSED": INK["violet"],
                       "DEBITED": INK["cyan"], "HANDOFF_UDIR": INK["red"]}  # fmt: skip
            fig = go.Figure(go.Pie(labels=dist.state, values=dist.n, hole=0.62,
                                   marker_colors=[colours.get(s, INK["slate"]) for s in dist.state]))  # fmt: skip
            fig.update_layout(height=280, showlegend=True)
            st.plotly_chart(fig, use_container_width=True)

    left, right = st.columns([3, 2])
    with left:
        ui.section("Recovery backlog", "ambiguous payments outstanding over time (ledger)")
        bl, _ = src.query(
            """WITH b AS (SELECT generate_series(date_trunc('minute', now()) - interval '30 min',
                                              now(), interval '15 seconds') AS t)
               SELECT b.t, count(x.transaction_id) AS backlog FROM b LEFT JOIN transactions x
                 ON x.experiment_id = :e AND x.ambiguous_since <= b.t
                AND (x.resolved_at IS NULL OR x.resolved_at > b.t)
               GROUP BY b.t ORDER BY b.t""",
            e=src.EXP,
        )
        if bl is None or bl.backlog.sum() == 0:
            ui.no_data("No ambiguous backlog in the last 30 minutes",
                       "A lost-confirmation payment creates one")  # fmt: skip
        else:
            fig = go.Figure(go.Scatter(x=bl.t, y=bl.backlog, mode="lines", fill="tozeroy",
                                       line=dict(color=INK["amber"], width=2)))  # fmt: skip
            fig.update_layout(height=250)
            st.plotly_chart(fig, use_container_width=True)
    with right:
        ui.section("Recent alerts", "untrusted telemetry and backend refusals")
        alerts, _ = src.query(
            """(SELECT window_end AS ts, 'telemetry ' || verdict AS alert, route_id AS subject,
                       cross_check AS detail
                FROM v11_telemetry_windows WHERE experiment_id = :e AND verdict <> 'TRUSTED'
                ORDER BY id DESC LIMIT 6)
               UNION ALL
               (SELECT ts, action || ' ' || decision, coalesce(route_id, transaction_id), reason
                FROM audit_events WHERE decision IN ('REJECTED', 'FALLBACK')
                ORDER BY seq DESC LIMIT 6)
               ORDER BY ts DESC LIMIT 8""",
            e=src.EXP,
        )
        if alerts is None or alerts.empty:
            ui.no_data("No alerts", "Flagged telemetry or refusals appear here")
        else:
            st.dataframe(alerts, hide_index=True, use_container_width=True, height=250)
    ui.source_line("LIVE: PostgreSQL (read-only role)", "controller /v1.1/introspection",
                   "control /api/v1/aorts/v11/summary", f"experiment {src.EXP}")  # fmt: skip
