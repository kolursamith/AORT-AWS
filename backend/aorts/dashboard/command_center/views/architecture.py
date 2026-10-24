"""PAGE 9: System architecture with live component health."""

from __future__ import annotations

import streamlit as st

from backend.aorts.dashboard.command_center import components as ui
from backend.aorts.dashboard.command_center import sources as src

DOT = """
digraph AORTS {
  graph [bgcolor="transparent", rankdir=TB, pad=0.3, nodesep=0.45, ranksep=0.5, fontname="Inter", fontsize=13];
  node [shape=box, style="rounded,filled", fontname="Inter", fontsize=13, margin="0.18,0.08", color="#1f2a44",
        fillcolor="#0e1526", fontcolor="#e6ecf7", penwidth=1.2];
  edge [color="#5d6a88", fontcolor="#8b97b3", fontname="Inter", fontsize=9, arrowsize=0.7];
  subgraph cluster_b { label="Track B: authoritative banking backend"; fontcolor="#8b97b3";
    color="#1f2a44"; style="rounded";
    rem [label="Remitter banks A1 · A2 · A3\\n(remitter service)", color="%(remitter)s"];
    sw [label="Payment switch\\nadmission: quotas, credits", color="%(switch)s"];
    bb [label="Beneficiary B", color="%(beneficiary-B)s"]; bc [label="Beneficiary C", color="%(beneficiary-C)s"];
    db [label="PostgreSQL ledger\\nCAS · double entry · evidence", shape=cylinder, color="#3b82f6"];
    ob [label="Event outbox +\\ntelemetry windows", color="#3b82f6"];
    disp [label="v1.1 dispatcher\\nexecution gate", color="#22d3ee"];
  }
  subgraph cluster_a { label="Track A: intelligence (computes, never moves money)"; fontcolor="#8b97b3";
    color="#1f2a44"; style="rounded";
    rt [label="Route Digital Twin", color="%(controller)s"]; tt [label="Transaction Digital Twin", color="%(controller)s"];
    ce [label="Trust-aware capacity\\nestimator"]; la [label="Feedback-aware\\nlook-ahead"];
    op [label="Safe budget optimizer\\n+ deadline ranker"];
  }
  rem -> sw [label="signed payment"]; sw -> bb; sw -> bc; rem -> db [label="debit, evidence"];
  db -> ob; ob -> rt [label="validated telemetry"]; ob -> tt [label="ledger events"];
  rt -> ce -> la -> op; tt -> op; op -> disp [label="RecoveryDecision v1.1"];
  disp -> rem [label="status inquiry"]; sw -> rem [label="signed status result", style=dashed];
  rem -> tt [label="status result", style=dashed];
}
"""


def render() -> None:
    ui.hero("SYSTEM ARCHITECTURE", "Two tracks, one protocol, one authoritative ledger",
            "Track B owns money and evidence; Track A owns models and decisions. They exchange "
            "only strict v1.1 messages, and every decision is re-gated by Track B.", ["LIVE"])  # fmt: skip
    h = src.health()
    colour = {k: ("#10b981" if v.get("http") == 200 else "#ef4444") for k, v in h.items()}
    for k in ("remitter", "switch", "beneficiary-B", "beneficiary-C", "controller"):
        colour.setdefault(k, "#64748b")
    st.graphviz_chart(DOT % colour, use_container_width=True)
    st.caption("Node outline: green = health endpoint answered 200 now, red = unreachable, "
               "slate = not checked from this process.")  # fmt: skip
    cols = st.columns(len(h))
    for col, (name, info) in zip(cols, h.items(), strict=True):
        col.markdown(f"**{name}**<br/>{ui.pill('HEALTHY' if info.get('http') == 200 else 'UNAVAILABLE')}",
                     unsafe_allow_html=True)  # fmt: skip
    c1, c2 = st.columns(2)
    with c1:
        ui.section("Route Digital Twin", "what the network is doing")
        st.markdown(
            "- Keeps only **authenticated, unflagged** telemetry from registered sources.\n"
            "- Tracks sequence per source; duplicates are idempotent, conflicts quarantine.\n"
            "- Freshness: `NO_DATA` / `FRESH` / `STALE`; it estimates nothing itself.\n"
            "- Feeds the Stage 06 estimator: a conservative capacity **lower bound** and safe "
            "headroom; zero when trust or evidence is missing."
        )  # fmt: skip
    with c2:
        ui.section("Transaction Digital Twin", "where each ambiguous payment stands")
        st.markdown(
            "- A **non-authoritative projection** of committed ledger events.\n"
            "- Financial outcome copied from the ledger; recovery workflow derived.\n"
            "- Computes timing-policy eligibility and deadline escalation.\n"
            "- Never debits, credits, reverses or sends a check."
        )  # fmt: skip
    ui.section("Research idea in one paragraph", "")
    ui.note("When a payment confirmation is lost, the remitter does not know whether the "
            "beneficiary credited the customer: the payment is <b>ambiguous</b> (UNKNOWN is not "
            "failure). Status inquiries can resolve it, but they share capacity with payments; "
            "under stress more inquiries can cause more timeouts and so more ambiguity. AORTS asks "
            "whether a trust-aware, uncertainty-aware, feedback-aware controller can choose safer "
            "inquiry budgets. The evaluation shows it never added ambiguity, but its conservative "
            "bound also left most recoverable payments unresolved (see Research analytics).")  # fmt: skip
