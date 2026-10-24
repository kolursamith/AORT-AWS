"""The dashboard's PostgreSQL role can read the ledger and cannot change it."""

from __future__ import annotations

import secrets

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from backend.aorts.integration import readonly_role
from tests.trackb.conftest import TEST_DB_URL, pay


@pytest.fixture
def dashboard_engine(stack, monkeypatch):
    password = secrets.token_urlsafe(24)
    monkeypatch.setenv("AORTS_DATABASE_URL", TEST_DB_URL)
    monkeypatch.setenv("AORTS_DASHBOARD_DB_PASSWORD", password)
    assert readonly_role.main() == 0
    assert readonly_role.main() == 0  # idempotent
    url = make_url(TEST_DB_URL).set(username=readonly_role.ROLE, password=password)
    eng = create_engine(url)
    yield eng
    eng.dispose()


def test_dashboard_role_reads_but_never_writes(stack, dashboard_engine):
    pay(stack, simulate="lost-response")  # a real ambiguous payment through the remitter API
    with dashboard_engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM transactions")).scalar() >= 1
        assert c.execute(text("SHOW default_transaction_read_only")).scalar() == "on"
    statements = [
        "UPDATE transactions SET state = 'CLOSED'",
        "DELETE FROM ledger_entries",
        "INSERT INTO audit_events (event_id) VALUES ('x')",
        "UPDATE accounts SET balance_paise = 0",
    ]
    for sql in statements:
        with dashboard_engine.connect() as c, pytest.raises(DBAPIError):
            c.execute(text("SET TRANSACTION READ WRITE"))  # even an explicit attempt fails
            c.execute(text(sql))


def test_role_creation_refuses_a_weak_or_missing_password(monkeypatch):
    monkeypatch.setenv("AORTS_DASHBOARD_DB_PASSWORD", "short")
    assert readonly_role.main() == 1
    monkeypatch.setenv("AORTS_DASHBOARD_DB_PASSWORD", "x" * 20 + "'; DROP TABLE t; --")
    assert readonly_role.main() == 1
