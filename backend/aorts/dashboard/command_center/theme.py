"""AORTS command-center visual identity: palette, CSS and the Plotly template.

Dark navy surfaces, charcoal panels, one electric-cyan accent, and status colours that always
mean the same thing (emerald = healthy/verified, amber = degraded/warming, red = fail-closed or
security alert, slate = unavailable). Charts use a fixed categorical order so a policy or route
keeps its colour on every page.
"""

from __future__ import annotations

import plotly.graph_objects as go
import plotly.io as pio
import streamlit as st

INK = {
    "bg": "#070b16",
    "panel": "#0e1526",
    "panel2": "#121b31",
    "line": "#1f2a44",
    "text": "#e6ecf7",
    "muted": "#8b97b3",
    "faint": "#5d6a88",
    "cyan": "#22d3ee",
    "blue": "#3b82f6",
    "violet": "#8b5cf6",
    "emerald": "#10b981",
    "amber": "#f59e0b",
    "red": "#ef4444",
    "slate": "#64748b",
}
# fixed categorical order (policies, routes, arms): cyan, blue, violet, emerald, amber, rose, ...
SERIES = ["#22d3ee", "#3b82f6", "#8b5cf6", "#10b981", "#f59e0b", "#f43f5e", "#a3e635",
          "#38bdf8", "#c084fc", "#fb923c", "#2dd4bf", "#e879f9", "#94a3b8"]  # fmt: skip

STATUS = {
    "HEALTHY": ("emerald", "Healthy"),
    "NORMAL": ("emerald", "Normal"),
    "TRUSTED": ("emerald", "Trusted"),
    "FRESH": ("emerald", "Fresh"),
    "VERIFIED": ("emerald", "Verified"),
    "OK": ("emerald", "OK"),
    "LIVE": ("cyan", "Live"),
    "RECORDED": ("violet", "Recorded evidence"),
    "MODEL PREDICTION": ("amber", "Model prediction"),
    "DEGRADED": ("amber", "Degraded"),
    "RECOVERING": ("amber", "Recovering"),
    "WARMING_UP": ("amber", "Warming up"),
    "INITIALIZING": ("amber", "Initialising"),
    "SUSPICIOUS": ("amber", "Suspicious"),
    "STALE": ("amber", "Stale"),
    "CONGESTED": ("red", "Congested"),
    "FAIL_CLOSED": ("red", "Fail-closed"),
    "REJECTED": ("red", "Rejected"),
    "FAILED": ("red", "Failed"),
    "UNAVAILABLE": ("slate", "Unavailable"),
    "NO_DATA": ("slate", "No data"),
}

_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');
:root { --bg:%(bg)s; --panel:%(panel)s; --panel2:%(panel2)s; --line:%(line)s; --text:%(text)s;
  --muted:%(muted)s; --faint:%(faint)s; --cyan:%(cyan)s; --blue:%(blue)s; --violet:%(violet)s;
  --emerald:%(emerald)s; --amber:%(amber)s; --red:%(red)s; --slate:%(slate)s; }
html, body, [data-testid="stAppViewContainer"], .stApp { background: radial-gradient(1200px 600px at 85%% -10%%, rgba(59,130,246,.10), transparent 60%%), radial-gradient(900px 500px at -10%% 10%%, rgba(139,92,246,.08), transparent 60%%), var(--bg) !important; color: var(--text); font-family: Inter, system-ui, sans-serif; }
[data-testid="stHeader"] { background: transparent; }
[data-testid="stSidebar"] { background: linear-gradient(180deg, #0a1122 0%%, #070b16 100%%); border-right: 1px solid var(--line); }
[data-testid="stSidebar"] * { color: var(--text); }
.block-container { padding-top: 1.6rem; padding-bottom: 3rem; max-width: 1480px; }
h1, h2, h3, h4 { font-family: Inter, system-ui, sans-serif; letter-spacing: -0.01em; color: var(--text); }
code, .mono { font-family: 'JetBrains Mono', ui-monospace, monospace; }
a { color: var(--cyan); }
.aorts-hero { padding: 22px 26px; border-radius: 18px; border: 1px solid var(--line); background: linear-gradient(135deg, rgba(34,211,238,.10), rgba(59,130,246,.07) 45%%, rgba(139,92,246,.10)); margin-bottom: 18px; animation: aortsFade .5s ease-out; }
.aorts-hero .kicker { color: var(--cyan); font-size: 12px; font-weight: 600; letter-spacing: .18em; text-transform: uppercase; }
.aorts-hero h1 { font-size: 30px; margin: 4px 0 2px 0; font-weight: 700; }
.aorts-hero .sub { color: var(--muted); font-size: 14px; }
.aorts-card { background: linear-gradient(180deg, var(--panel2), var(--panel)); border: 1px solid var(--line); border-radius: 14px; padding: 14px 16px; height: 100%%; transition: border-color .2s ease, transform .2s ease; animation: aortsFade .4s ease-out; }
.aorts-card:hover { border-color: rgba(34,211,238,.45); transform: translateY(-1px); }
.aorts-card .label { color: var(--muted); font-size: 11.5px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase; }
.aorts-card .value { font-size: 26px; font-weight: 700; margin-top: 4px; font-variant-numeric: tabular-nums; }
.aorts-card .sub { color: var(--faint); font-size: 12px; margin-top: 2px; }
.aorts-card.small .value { font-size: 17px; line-height: 1.35; padding-top: 4px; word-break: break-word; }
.aorts-card.tone-emerald .value { color: var(--emerald); } .aorts-card.tone-amber .value { color: var(--amber); }
.aorts-card.tone-red .value { color: var(--red); } .aorts-card.tone-cyan .value { color: var(--cyan); }
.aorts-card.tone-slate .value { color: var(--slate); }
.aorts-pill { display:inline-flex; align-items:center; gap:6px; padding: 3px 10px; border-radius: 999px; font-size: 11.5px; font-weight: 600; letter-spacing:.03em; border: 1px solid; white-space: nowrap; }
.aorts-pill::before { content:''; width:7px; height:7px; border-radius:50%%; background: currentColor; box-shadow: 0 0 8px currentColor; }
.pill-emerald { color: var(--emerald); border-color: rgba(16,185,129,.4); background: rgba(16,185,129,.10); }
.pill-amber { color: var(--amber); border-color: rgba(245,158,11,.4); background: rgba(245,158,11,.10); }
.pill-red { color: var(--red); border-color: rgba(239,68,68,.45); background: rgba(239,68,68,.10); }
.pill-cyan { color: var(--cyan); border-color: rgba(34,211,238,.45); background: rgba(34,211,238,.08); }
.pill-violet { color: #a78bfa; border-color: rgba(139,92,246,.45); background: rgba(139,92,246,.10); }
.pill-slate { color: #94a3b8; border-color: rgba(100,116,139,.45); background: rgba(100,116,139,.10); }
.aorts-section { margin: 26px 0 10px 0; display:flex; align-items:baseline; justify-content:space-between; gap: 12px; border-bottom: 1px solid var(--line); padding-bottom: 6px; }
.aorts-section h3 { font-size: 17px; margin: 0; font-weight: 600; }
.aorts-section .cap { color: var(--muted); font-size: 12.5px; }
.aorts-note { border-left: 3px solid var(--cyan); background: rgba(34,211,238,.06); padding: 10px 14px; border-radius: 0 10px 10px 0; color: var(--text); font-size: 13.5px; margin: 8px 0; }
.aorts-note.warn { border-left-color: var(--amber); background: rgba(245,158,11,.07); }
.aorts-note.neg { border-left-color: var(--red); background: rgba(239,68,68,.07); }
.aorts-empty { border: 1px dashed var(--line); border-radius: 12px; padding: 18px; color: var(--muted); text-align:center; font-size: 13.5px; }
.aorts-empty b { color: var(--text); letter-spacing:.08em; }
.aorts-pipe { display:flex; flex-wrap: wrap; align-items:stretch; gap: 8px; margin: 6px 0 4px 0; }
.aorts-step { flex: 1 1 130px; min-width: 120px; background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 10px 12px; position: relative; }
.aorts-step .n { color: var(--cyan); font-size: 11px; font-weight: 700; letter-spacing: .12em; }
.aorts-step .t { font-size: 13px; font-weight: 600; margin-top: 2px; }
.aorts-step .v { color: var(--muted); font-size: 12px; margin-top: 4px; font-family: 'JetBrains Mono', monospace; }
.aorts-step.ok { border-color: rgba(16,185,129,.5); } .aorts-step.warn { border-color: rgba(245,158,11,.55); }
.aorts-step.bad { border-color: rgba(239,68,68,.55); }
.aorts-arrow { align-self:center; color: var(--faint); font-size: 18px; }
.aorts-tl { position: relative; margin-left: 10px; border-left: 2px solid var(--line); padding-left: 16px; }
.aorts-tl .ev { position: relative; margin: 0 0 12px 0; }
.aorts-tl .ev::before { content:''; position:absolute; left:-23px; top:5px; width:10px; height:10px; border-radius:50%%; background: var(--cyan); box-shadow: 0 0 10px var(--cyan); }
.aorts-tl .ev.ok::before { background: var(--emerald); box-shadow: 0 0 10px var(--emerald); }
.aorts-tl .ev.warn::before { background: var(--amber); box-shadow: 0 0 10px var(--amber); }
.aorts-tl .ev.bad::before { background: var(--red); box-shadow: 0 0 10px var(--red); }
.aorts-tl .ev .h { font-weight: 600; font-size: 13.5px; } .aorts-tl .ev .m { color: var(--muted); font-size: 12px; font-family:'JetBrains Mono', monospace; }
.aorts-brand { padding: 6px 4px 14px 4px; } .aorts-brand .logo { font-size: 22px; font-weight: 800; letter-spacing: .14em; background: linear-gradient(90deg, var(--cyan), var(--blue), var(--violet)); -webkit-background-clip: text; background-clip: text; color: transparent; }
.aorts-brand .tag { color: var(--muted); font-size: 11.5px; line-height: 1.35; margin-top: 2px; }
div[data-testid="stMetric"] { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 10px 14px; }
div[data-testid="stDataFrame"], div[data-testid="stTable"] { border: 1px solid var(--line); border-radius: 12px; overflow: hidden; }
.stTabs [data-baseweb="tab-list"] { gap: 6px; } .stTabs [data-baseweb="tab"] { background: var(--panel); border: 1px solid var(--line); border-radius: 10px 10px 0 0; padding: 6px 14px; }
.stTabs [aria-selected="true"] { border-color: rgba(34,211,238,.6) !important; color: var(--cyan) !important; }
.stButton > button { border-radius: 10px; border: 1px solid rgba(34,211,238,.45); background: rgba(34,211,238,.08); color: var(--text); font-weight: 600; }
.stButton > button:hover { border-color: var(--cyan); color: var(--cyan); }
@keyframes aortsFade { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }
@media (prefers-reduced-motion: reduce) { .aorts-card, .aorts-hero { animation: none; transition: none; } }
</style>
"""


def inject() -> None:
    st.markdown(_CSS % INK, unsafe_allow_html=True)


def register_plotly_template() -> None:
    if "aorts" in pio.templates:
        pio.templates.default = "aorts"
        return
    tmpl = go.layout.Template(
        layout=go.Layout(
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(14,21,38,0.65)",
            font=dict(family="Inter, system-ui, sans-serif", color=INK["text"], size=12),
            colorway=SERIES,
            xaxis=dict(gridcolor=INK["line"], zerolinecolor=INK["line"], linecolor=INK["line"]),
            yaxis=dict(gridcolor=INK["line"], zerolinecolor=INK["line"], linecolor=INK["line"]),
            legend=dict(
                bgcolor="rgba(0,0,0,0)",
                orientation="h",
                y=1.02,
                yanchor="bottom",
                x=1,
                xanchor="right",
            ),
            margin=dict(l=48, r=18, t=48, b=44),
            hoverlabel=dict(bgcolor=INK["panel2"], bordercolor=INK["cyan"]),
        )
    )
    pio.templates["aorts"] = tmpl
    pio.templates.default = "aorts"
