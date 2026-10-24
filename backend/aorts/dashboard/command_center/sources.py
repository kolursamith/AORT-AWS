"""Genuine data sources for the command center (read-only; short-TTL caches).

LIVE sources
    * PostgreSQL through the ``aorts_dashboard`` role: SELECT-only grants and every connection
      forced to ``default_transaction_read_only``. The dashboard cannot write the ledger.
    * Track A controller ``GET /v1.1/introspection`` and ``/v1.1/health`` (bearer token).
    * Track B control API (health, the v1.1 security summary, faults; admin endpoints only for
      the operator actions on the demo page).
    * Service ``/api/v1/health`` endpoints.
RECORDED sources
    * Stage 10 security evidence, Stage 11 end-to-end evidence, Stage 12 research results and
      Stage 13 demo runs, read from the repository's ``docs/`` (mounted read-only).

Every loader returns ``(data, error)``; pages show DATA UNAVAILABLE with the error instead of
substituting numbers.
"""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path

import httpx
import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, event, text

EXP = os.getenv("AORTS_EXPERIMENT_ID", "aorts-s11-demo")
DOCS = Path(os.getenv("AORTS_DOCS_DIR", Path(__file__).resolve().parents[4] / "docs"))
SERVICES = {
    "remitter": os.getenv("AORTS_REMITTER_URL", "http://127.0.0.1:18100"),
    "switch": os.getenv("AORTS_SWITCH_URL", "http://127.0.0.1:18110"),
    "beneficiary-B": "http://beneficiary-b:8121" if os.getenv("AORTS_IN_DOCKER") else "",
    "beneficiary-C": "http://beneficiary-c:8122" if os.getenv("AORTS_IN_DOCKER") else "",
    "control": os.getenv("AORTS_CONTROL_URL", "http://127.0.0.1:18130"),
}
CONTROLLER = os.getenv("AORTS_V11_CONTROLLER_URL", "http://127.0.0.1:18140")
TTL = 4  # seconds: bounded load on PostgreSQL and the controller


# ------------------------------------------------------------------------- database


@st.cache_resource
def _engine():
    url = os.getenv("AORTS_DASHBOARD_DATABASE_URL")
    if not url:
        return None
    eng = create_engine(url, pool_size=3, max_overflow=2, pool_pre_ping=True)

    @event.listens_for(eng, "connect")
    def _read_only(dbapi_conn, _record):  # belt and braces beside the role default
        cur = dbapi_conn.cursor()
        cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        cur.close()
        dbapi_conn.commit()  # SET is transactional in PostgreSQL: keep it

    return eng


@st.cache_data(ttl=TTL, show_spinner=False)
def query(sql: str, **params) -> tuple[pd.DataFrame | None, str | None]:
    eng = _engine()
    if eng is None:
        return None, "AORTS_DASHBOARD_DATABASE_URL is not configured"
    try:
        with eng.connect() as c:
            return pd.read_sql(text(sql), c, params=params), None
    except Exception as exc:  # noqa: BLE001 - shown to the operator as DATA UNAVAILABLE
        return None, f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"


def scalar(sql: str, **params):
    df, err = query(sql, **params)
    if err or df is None or df.empty:
        return None
    return df.iat[0, 0]


# ---------------------------------------------------------------------------- HTTP


@st.cache_data(ttl=TTL, show_spinner=False)
def controller(path: str = "/v1.1/introspection") -> tuple[dict | None, str | None]:
    token = os.getenv("AORTS_BACKEND_TOKEN")
    if not token:
        return None, "AORTS_BACKEND_TOKEN is not configured"
    try:
        r = httpx.get(CONTROLLER + path, headers={"Authorization": f"Bearer {token}"}, timeout=4)
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}: {r.text[:160]}"
        return r.json(), None
    except httpx.HTTPError as exc:
        return None, f"{type(exc).__name__}: controller unreachable at {CONTROLLER}"


@st.cache_data(ttl=TTL, show_spinner=False)
def health() -> dict[str, dict]:
    out = {}
    for name, base in SERVICES.items():
        if not base:
            continue
        try:
            r = httpx.get(base + "/api/v1/health", timeout=2)
            out[name] = {"http": r.status_code, "body": r.json() if r.status_code == 200 else None}
        except httpx.HTTPError as exc:
            out[name] = {"http": None, "error": type(exc).__name__}
    data, err = controller("/v1.1/health")
    out["controller"] = {"http": 200 if data else None, "body": data, "error": err}
    return out


def control_client():
    from backend.aorts.dashboard.api_client import ControlClient

    return ControlClient(SERVICES["control"], os.getenv("AORTS_ADMIN_TOKEN"))


@st.cache_data(ttl=TTL, show_spinner=False)
def v11_summary() -> tuple[dict | None, str | None]:
    try:
        return control_client().aorts_v11(EXP), None
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: control API unavailable"


# ------------------------------------------------------------------- recorded evidence


@st.cache_data(ttl=600, show_spinner=False)
def stage10_runs(kind: str) -> tuple[pd.DataFrame | None, str | None]:
    folder = DOCS / "integration" / "evidence" / "stage10" / kind
    files = sorted(folder.glob("secpkg-*-runs.csv"))
    if not files:
        return None, f"no Stage 10 {kind} runs CSV under {folder}"
    with files[-1].open(encoding="utf-8") as f:
        df = pd.DataFrame(list(csv.DictReader(f)))
    df = df[df["status"] == "completed"]
    for col in df.columns:
        conv = pd.to_numeric(df[col], errors="coerce")
        if conv.notna().sum() >= max(1, df[col].ne("").sum() * 0.8):
            df[col] = conv
    df.attrs["source"] = str(files[-1].relative_to(DOCS.parent))
    return df, None


@st.cache_data(ttl=600, show_spinner=False)
def stage12() -> tuple[dict | None, pd.DataFrame | None, str | None]:
    base = DOCS / "research" / "stage12"
    try:
        summary = json.loads((base / "summary.json").read_text(encoding="utf-8"))
        flat = pd.read_csv(base / "runs_flat.csv")
        return summary, flat, None
    except (OSError, ValueError) as exc:
        return None, None, f"{type(exc).__name__}: {base}"


@st.cache_data(ttl=600, show_spinner=False)
def doc_json(rel: str) -> tuple[dict | list | None, str | None]:
    path = DOCS / rel
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {path}"


def doc_text(rel: str) -> str | None:
    try:
        return (DOCS / rel).read_text(encoding="utf-8")
    except OSError:
        return None
