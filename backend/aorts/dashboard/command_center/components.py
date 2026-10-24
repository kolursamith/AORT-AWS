"""Reusable presentation components (HTML is escaped; every value comes from the caller)."""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape

import streamlit as st

from backend.aorts.dashboard.command_center.theme import STATUS


def _e(value) -> str:
    return escape("" if value is None else str(value))


def hero(kicker: str, title: str, sub: str, badges: list[str] | None = None) -> None:
    pills = " ".join(pill(b) for b in badges or [])
    st.markdown(
        f'<div class="aorts-hero"><div class="kicker">{_e(kicker)}</div><h1>{_e(title)}</h1>'
        f'<div class="sub">{_e(sub)}</div><div style="margin-top:10px">{pills}</div></div>',
        unsafe_allow_html=True,
    )


def pill(state, label: str | None = None) -> str:
    key = str(state or "UNAVAILABLE").upper()
    tone, text = STATUS.get(key, ("slate", key.replace("_", " ").title()))
    return f'<span class="aorts-pill pill-{tone}">{_e(label or text)}</span>'


def kpi(label: str, value, sub: str = "", tone: str = "") -> str:
    shown = "—" if value is None else value
    if isinstance(shown, float):
        shown = f"{shown:,.2f}"
    elif isinstance(shown, int):
        shown = f"{shown:,}"
    cls = f" tone-{tone}" if tone else ""
    if isinstance(shown, str) and len(shown) > 9:
        cls += " small"
    return (
        f'<div class="aorts-card{cls}"><div class="label">{_e(label)}</div>'
        f'<div class="value">{_e(shown)}</div><div class="sub">{_e(sub)}</div></div>'
    )


def kpi_row(cards: list[str]) -> None:
    cols = st.columns(len(cards))
    for col, card in zip(cols, cards, strict=True):
        col.markdown(card, unsafe_allow_html=True)


def section(title: str, caption: str = "", badge: str | None = None) -> None:
    right = pill(badge) if badge else f'<span class="cap">{_e(caption)}</span>'
    st.markdown(
        f'<div class="aorts-section"><h3>{_e(title)}</h3>{right}</div>', unsafe_allow_html=True
    )
    if badge and caption:
        st.caption(caption)


def note(text: str, kind: str = "") -> None:
    st.markdown(f'<div class="aorts-note {kind}">{text}</div>', unsafe_allow_html=True)


def empty(what: str, source: str) -> None:
    st.markdown(
        f'<div class="aorts-empty"><b>DATA UNAVAILABLE</b><br/>{_e(what)}'
        f'<br/><span style="color:var(--faint)">source: {_e(source)}</span></div>',
        unsafe_allow_html=True,
    )


def no_data(what: str, hint: str) -> None:
    st.markdown(
        f'<div class="aorts-empty"><b>NO DATA YET</b><br/>{_e(what)}'
        f'<br/><span style="color:var(--faint)">{_e(hint)}</span></div>',
        unsafe_allow_html=True,
    )


def pipeline(steps: list[tuple[str, str, str]]) -> None:
    """``steps``: (title, value text, tone ok|warn|bad|'') rendered as connected stages."""

    parts = []
    for i, (title, value, tone) in enumerate(steps):
        if i:
            parts.append('<div class="aorts-arrow">&rarr;</div>')
        parts.append(
            f'<div class="aorts-step {tone}"><div class="n">STEP {i + 1}</div>'
            f'<div class="t">{_e(title)}</div><div class="v">{_e(value)}</div></div>'
        )
    st.markdown(f'<div class="aorts-pipe">{"".join(parts)}</div>', unsafe_allow_html=True)


def timeline(events: list[tuple[str, str, str]]) -> None:
    """``events``: (heading, meta line, tone ok|warn|bad|'')."""

    rows = "".join(
        f'<div class="ev {tone}"><div class="h">{_e(h)}</div><div class="m">{_e(m)}</div></div>'
        for h, m, tone in events
    )
    st.markdown(f'<div class="aorts-tl">{rows}</div>', unsafe_allow_html=True)


def when(ts) -> str:
    """Timezone-aware display (UTC) and age."""

    if ts is None:
        return "—"
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except ValueError:
            return ts
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    age = (datetime.now(UTC) - ts).total_seconds()
    ago = f"{age:.0f}s ago" if age < 120 else f"{age / 60:.0f} min ago" if age < 7200 else ""
    return f"{ts.astimezone(UTC):%Y-%m-%d %H:%M:%S} UTC" + (f" · {ago}" if ago else "")


def source_line(*items: str) -> None:
    st.caption(" · ".join(items))
