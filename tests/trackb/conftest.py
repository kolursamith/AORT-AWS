"""Test fixtures.

Tests need a PostgreSQL server (row locks, unique constraints and SKIP LOCKED are
part of what is being tested; SQLite would hide real concurrency bugs).
Default: ``postgresql+psycopg://aorts:aorts_local_dev@127.0.0.1:55432/aorts_test``,
override with ``AORTS_TEST_DATABASE_URL``. The test database is dropped and
re-created through Alembic once per session, so migrations are tested too.
"""

from __future__ import annotations

import os
import tempfile
import uuid

# Lab experiments triggered by tests must not mix with real results in results/.
os.environ.setdefault("AORTS_RESULTS_DIR", tempfile.mkdtemp(prefix="aorts-test-results-"))

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from backend.aorts.app.config import Settings
from backend.aorts.app.db import Database
from backend.aorts.app.local_stack import build_local_stack, generate_service_keys, reset_database

ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)  # repository root


def _default_test_url() -> str:
    """Same server as AORTS_DATABASE_URL in .env (the Compose Postgres), but database ``aorts_test``."""
    env_file = os.path.join(ROOT, ".env")
    url = os.environ.get("AORTS_DATABASE_URL")
    if not url and os.path.exists(env_file):
        for line in open(env_file, encoding="utf-8"):
            if line.startswith("AORTS_DATABASE_URL="):
                url = line.split("=", 1)[1].strip()
    if not url:
        url = "postgresql+psycopg://aorts:aorts_local_dev@127.0.0.1:55432/aorts"
    return make_url(url).set(database="aorts_test").render_as_string(hide_password=False)


TEST_DB_URL = os.environ.get("AORTS_TEST_DATABASE_URL") or _default_test_url()
OPENING = 1_000_000_00  # 10 lakh rupees per synthetic customer, in paise
# Pinned per process: pytest may import this module under two names (conftest + helper import).
ADMIN_TOKEN = os.environ.setdefault(
    "AORTS_TEST_ADMIN_TOKEN", "test-admin-token-" + uuid.uuid4().hex
)


SESSION_LOCK = (
    7_420_778  # advisory lock on the server's "postgres" database: one test session per server
)


def _acquire_session_lock(url: str):
    """Hold a server-wide lock for the session.

    The session fixture drops the test database WITH (FORCE), which kills every other session's
    connections; a second concurrent pytest run must therefore fail fast instead of corrupting
    the first one.
    """
    u = make_url(url)
    engine = create_engine(
        u.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        connect_args={"connect_timeout": 5},
    )
    conn = engine.connect()
    if not conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": SESSION_LOCK}).scalar():
        conn.close()
        engine.dispose()
        pytest.fail(
            "another Track B test session is using this PostgreSQL server; run one session at a time"
        )
    return engine, conn


def _recreate_database(url: str) -> None:
    u = make_url(url)
    admin = create_engine(
        u.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        connect_args={"connect_timeout": 5},
    )
    try:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{u.database}" WITH (FORCE)'))
            c.execute(text(f'CREATE DATABASE "{u.database}"'))
    finally:
        admin.dispose()


@pytest.fixture(scope="session")
def database() -> Database:
    try:
        lock_engine, lock_conn = _acquire_session_lock(TEST_DB_URL)
        _recreate_database(TEST_DB_URL)
    except pytest.fail.Exception:
        raise
    except Exception as exc:  # noqa: BLE001
        msg = (
            f"PostgreSQL test server not reachable ({type(exc).__name__}); start it with `make db` "
            f"or set AORTS_TEST_DATABASE_URL"
        )
        if (
            os.environ.get("AORTS_REQUIRE_DB") == "1"
        ):  # set by make/aorts.ps1: never pass by skipping
            pytest.fail(msg)
        pytest.skip(msg)
    cfg = Config(os.path.join(ROOT, "backend", "aorts", "alembic.ini"))
    cfg.set_main_option(
        "script_location", os.path.join(ROOT, "backend", "aorts", "banking", "ledger", "migrations")
    )
    cfg.cmd_opts = type("o", (), {"x": [f"url={TEST_DB_URL}"]})()
    command.upgrade(cfg, "head")
    db = Database(TEST_DB_URL, pool_size=20)
    yield db
    db.dispose()
    lock_conn.close()  # closing the connection releases the advisory lock
    lock_engine.dispose()


@pytest.fixture(scope="session")
def service_keys() -> str:
    return generate_service_keys()


def make_settings(service_keys: str, **overrides) -> Settings:
    base = dict(
        environment="test",
        database_url=TEST_DB_URL,
        service_keys=service_keys,
        admin_token=ADMIN_TOKEN,
        enable_attack_controls=True,
        enable_fault_controls=True,
        beneficiary_urls="B=http://testserver,C=http://testserver",
        remitter_switch_timeout_s=2.0,
        switch_beneficiary_timeout_s=1.0,
        deadline_seconds=120,
        check_backoff_base_seconds=0.0,
        check_backoff_max_seconds=0.0,
        switch_queue_wait_s=0.2,
        accounts_seed_balance_paise=OPENING,
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)


@pytest.fixture
def settings(service_keys) -> Settings:
    return make_settings(service_keys)


@pytest.fixture
def clean_db(database, settings):
    reset_database(database, OPENING)
    return database


@pytest.fixture
def stack(clean_db, settings):
    st = build_local_stack(settings, clean_db)
    yield st
    st.close()


@pytest.fixture
def stack_factory(clean_db, service_keys):
    """Build a stack with custom settings (e.g. auto-reverse off, tight rate limits)."""
    built = []

    def _make(with_control=False, **overrides):
        st = build_local_stack(
            make_settings(service_keys, **overrides), clean_db, with_control=with_control
        )
        built.append(st)
        return st

    yield _make
    for st in built:
        st.close()


def pay(
    stack,
    sender="A1-CUST-0001",
    beneficiary="B-CUST-0001",
    bank="B",
    amount=10_000,
    key=None,
    simulate=None,
    admin=True,
):
    headers = {}
    if simulate:
        headers["X-AORTS-Simulate"] = simulate
        if admin:
            headers["X-AORTS-Admin-Token"] = ADMIN_TOKEN
    body = {
        "sender_account_id": sender,
        "beneficiary_account_id": beneficiary,
        "beneficiary_bank_id": bank,
        "amount_paise": amount,
        "idempotency_key": key or f"idem-{uuid.uuid4().hex}",
    }
    return stack.client.post(
        "remitter", "/api/v1/payments", body, as_identity="CHANNEL", headers=headers
    )


def balance(db: Database, account_id: str) -> int:
    with db.session() as s:
        return s.execute(
            text("SELECT balance_paise FROM accounts WHERE account_id=:a"), {"a": account_id}
        ).scalar()


def count(db: Database, sql: str, **params) -> int:
    with db.session() as s:
        return s.execute(text(sql), params).scalar()
