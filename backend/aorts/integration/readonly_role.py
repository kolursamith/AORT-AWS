"""Create (idempotently) the dashboard's read-only PostgreSQL role.

Run by the Compose ``migrate`` service after the migrations:

    python -m backend.aorts.integration.readonly_role

The role gets ``CONNECT``, schema ``USAGE`` and ``SELECT`` on every table (now and later), and
``default_transaction_read_only = on``. It owns nothing and has no write privilege, so the
dashboard cannot change the ledger even if its own code tried. The password comes from
``AORTS_DASHBOARD_DB_PASSWORD`` (environment only, never printed).
"""

from __future__ import annotations

import os
import re
import sys

from sqlalchemy import create_engine, text

ROLE = "aorts_dashboard"


def main() -> int:
    password = os.environ.get("AORTS_DASHBOARD_DB_PASSWORD", "")
    if len(password) < 16:
        print("AORTS_DASHBOARD_DB_PASSWORD is missing or too short; read-only role not created")
        return 1
    if not re.fullmatch(r"[A-Za-z0-9_-]+", password):
        print("AORTS_DASHBOARD_DB_PASSWORD must be URL-safe base64 (generate_env.py makes one)")
        return 1
    engine = create_engine(os.environ["AORTS_DATABASE_URL"], isolation_level="AUTOCOMMIT")
    with engine.connect() as c:
        db = c.execute(text("SELECT current_database()")).scalar()
        exists = c.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": ROLE}).scalar()
        verb = "ALTER" if exists else "CREATE"
        # identifiers cannot be bound parameters; ROLE and db are fixed / server-provided
        c.execute(text(f"{verb} ROLE {ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                       f"PASSWORD '{password}'"))  # fmt: skip
        c.execute(text(f'GRANT CONNECT ON DATABASE "{db}" TO {ROLE}'))
        c.execute(text(f"GRANT USAGE ON SCHEMA public TO {ROLE}"))
        c.execute(text(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {ROLE}"))
        c.execute(
            text(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {ROLE}")
        )
        c.execute(text(f"ALTER ROLE {ROLE} SET default_transaction_read_only = on"))
    engine.dispose()
    print(f"read-only role {ROLE} ready (SELECT only, read-only transactions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
