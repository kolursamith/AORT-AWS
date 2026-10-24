"""AORTS command center: Streamlit entry point.

    streamlit run backend/aorts/dashboard/command_center/app.py

Ten pages, one theme, genuine sources only (see ``sources.py``). Synthetic mock environment.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:  # `streamlit run` puts only this file's folder on sys.path
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from backend.aorts.dashboard.command_center import sources as src  # noqa: E402
from backend.aorts.dashboard.command_center import theme  # noqa: E402
from backend.aorts.dashboard.command_center.views import (  # noqa: E402
    architecture,
    controller,
    demo,
    ledger,
    overview,
    research,
    route_twin,
    security,
    transactions,
    txn_twin,
)

st.set_page_config(page_title="AORTS Command Center", page_icon=":material/hub:", layout="wide",
                   initial_sidebar_state="expanded")  # fmt: skip
theme.inject()
theme.register_plotly_template()

with st.sidebar:
    st.markdown(
        '<div class="aorts-brand"><div class="logo">AORTS</div><div class="tag">Adaptive '
        "Optimized Recovery Traffic System<br/>Intelligent Payment Recovery &amp; Security "
        "Operations</div></div>",
        unsafe_allow_html=True,
    )

pages = {
    "Operations": [
        st.Page(overview.render, title="Executive overview", icon=":material/dashboard:",
                url_path="overview", default=True),
        st.Page(transactions.render, title="Live transactions", icon=":material/receipt_long:",
                url_path="transactions"),
        st.Page(ledger.render, title="Ledger & audit", icon=":material/account_balance:",
                url_path="ledger"),
    ],
    "Intelligence": [
        st.Page(route_twin.render, title="Route digital twin", icon=":material/route:",
                url_path="route-twin"),
        st.Page(txn_twin.render, title="Transaction digital twin", icon=":material/timeline:",
                url_path="transaction-twin"),
        st.Page(controller.render, title="Intelligence engine", icon=":material/psychology:",
                url_path="intelligence"),
    ],
    "Security & research": [
        st.Page(security.render, title="Security operations", icon=":material/shield:",
                url_path="security"),
        st.Page(research.render, title="Research analytics", icon=":material/insights:",
                url_path="research"),
    ],
    "Project": [
        st.Page(architecture.render, title="System architecture", icon=":material/schema:",
                url_path="architecture"),
        st.Page(demo.render, title="Demo control", icon=":material/play_circle:",
                url_path="demo"),
    ],
}  # fmt: skip
nav = st.navigation(pages)

with st.sidebar:
    st.caption(f"Experiment `{src.EXP}`")
    auto = st.toggle("Auto-refresh (10 s)", value=False,
                     help="re-reads live sources every 10 s; caches keep the load bounded")  # fmt: skip
    st.caption("LIVE = running stack · RECORDED = saved evidence · MODEL PREDICTION = forecast")
    st.caption("Synthetic mock environment. Not connected to any real bank, NPCI/UPI or customer.")

nav.run()

if auto:
    import time

    time.sleep(10)
    st.rerun()
