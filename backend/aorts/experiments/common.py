"""Shared helpers for the experiment scripts.

Two modes:
* ``inprocess`` - builds the full stack in this process (real HTTP handlers, real
  signing, real PostgreSQL) on a dedicated database ``aorts_experiments`` that is
  wiped at the start of every run. Needs only the Compose Postgres (`make db`).
* ``http``      - talks to the running Docker Compose stack on localhost.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import statistics
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from backend.aorts.app.config import Settings
from backend.aorts.app.db import Database
from backend.aorts.app.http_client import SignedClient
from backend.aorts.app.local_stack import build_local_stack, reset_database
from backend.aorts.security.request_auth import KeyRegistry

ROOT = Path(__file__).resolve().parents[3]  # repository root
EXPERIMENT_LOCK = (
    7_420_777  # pg advisory lock key held for the duration of an in-process experiment
)
RESULTS = ROOT / "results"


def parser(description: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=description)
    p.add_argument("--mode", choices=["inprocess", "http"], default="inprocess")
    p.add_argument("--repeats", type=int, default=5)
    p.add_argument("--seed", type=int, default=42)
    return p


def experiment_id(name: str) -> str:
    return f"{name}-{datetime.now(UTC):%Y%m%dT%H%M%S}"


def base_settings(**overrides) -> Settings:
    return Settings(**overrides)


def _ensure_database(url: str) -> None:
    u = make_url(url)
    admin = create_engine(u.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        if not c.execute(
            text("SELECT 1 FROM pg_database WHERE datname=:d"), {"d": u.database}
        ).scalar():
            c.execute(text(f'CREATE DATABASE "{u.database}"'))
    admin.dispose()
    cfg = Config(str(ROOT / "backend" / "aorts" / "alembic.ini"))
    cfg.set_main_option(
        "script_location", str(ROOT / "backend" / "aorts" / "banking" / "ledger" / "migrations")
    )
    cfg.cmd_opts = type("o", (), {"x": [f"url={url}"]})()
    command.upgrade(cfg, "head")


@contextmanager
def inprocess_stack(**overrides):
    """Fresh stack on the dedicated experiments database (synthetic data is wiped first)."""
    s = Settings()
    url = make_url(s.database_url.get_secret_value()).set(database="aorts_experiments")
    url_s = url.render_as_string(hide_password=False)
    _ensure_database(url_s)
    settings = Settings(
        database_url=url_s,
        environment="experiment",
        enable_attack_controls=True,
        enable_fault_controls=True,
        beneficiary_urls="B=http://testserver,C=http://testserver",
        **overrides,
    )
    # One experiment at a time: every run wipes this database, so concurrent runs would corrupt each other.
    guard_engine = create_engine(url_s, pool_size=1)
    guard = guard_engine.connect()
    if not guard.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": EXPERIMENT_LOCK}).scalar():
        guard.close()
        guard_engine.dispose()
        raise RuntimeError(
            "another in-process experiment is already running on aorts_experiments; "
            "run experiments one at a time"
        )
    db = Database(url_s, pool_size=30)
    try:
        reset_database(db, settings.accounts_seed_balance_paise)
        stack = build_local_stack(settings, db, with_control=True)
        try:
            yield stack
        finally:
            stack.close()
    finally:
        db.dispose()
        guard.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": EXPERIMENT_LOCK})
        guard.close()
        guard_engine.dispose()


def http_client() -> tuple[SignedClient, Settings]:
    s = Settings()
    registry = KeyRegistry.parse(s.service_keys.get_secret_value(), s.signing_key_map())
    urls = {
        "remitter": s.remitter_url,
        "switch": s.switch_url,
        "control": s.control_url,
        **{f"beneficiary:{k}": v for k, v in s.beneficiary_url_map().items()},
    }
    return SignedClient(registry, urls, default_timeout=10.0), s


def write_outputs(name: str, payload: dict, rows: list[dict] | None = None) -> dict:
    (RESULTS / "raw").mkdir(parents=True, exist_ok=True)
    (RESULTS / "processed").mkdir(parents=True, exist_ok=True)
    raw = RESULTS / "raw" / f"{name}.json"
    raw.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    out = {"raw": str(raw)}
    if rows:
        csv_path = RESULTS / "processed" / f"{name}.csv"
        keys = sorted({k for r in rows for k in r})
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(rows)
        out["csv"] = str(csv_path)
    bucket = os.environ.get("AORTS_S3_RESULTS_BUCKET")
    if bucket:  # in AWS the experiment task uploads its outputs (task role grants s3:PutObject)
        import boto3

        s3 = boto3.client("s3")
        for path in out.values():
            s3.upload_file(path, bucket, f"experiments/{Path(path).parent.name}/{Path(path).name}")
        out["s3_bucket"] = bucket
    return out


def stats(values: list[float]) -> dict:
    """Mean, sample std and a normal-approximation 95% CI (t-based for small n)."""
    vals = [v for v in values if v is not None and not (isinstance(v, float) and math.isnan(v))]
    n = len(vals)
    if n == 0:
        return {"n": 0, "mean": None, "std": None, "ci95": None}
    mean = statistics.fmean(vals)
    if n == 1:
        return {"n": 1, "mean": round(mean, 4), "std": None, "ci95": None}
    sd = statistics.stdev(vals)
    t = {
        2: 12.706,
        3: 4.303,
        4: 3.182,
        5: 2.776,
        6: 2.571,
        7: 2.447,
        8: 2.365,
        9: 2.306,
        10: 2.262,
    }.get(n, 1.96)
    half = t * sd / math.sqrt(n)
    return {
        "n": n,
        "mean": round(mean, 4),
        "std": round(sd, 4),
        "ci95": [round(mean - half, 4), round(mean + half, 4)],
    }


def env_info(mode: str) -> dict:
    import platform

    return {
        "mode": mode,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "label": "synthetic mock environment",
    }
