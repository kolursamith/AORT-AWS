"""Reading banking state directly from the ledger database.

Used to check what recovery actually preserved: row counts before and after,
and whether the double-entry ledger still balances.
"""

from __future__ import annotations

import subprocess

BANKING_TABLES = ("m_client", "m_savings_account", "m_loan", "acc_gl_journal_entry")

# Fineract's journal entries: type_enum 2 = debit, 1 = credit.
DEBITS_SQL = "SELECT COALESCE(sum(amount),0) FROM acc_gl_journal_entry WHERE type_enum=2"
CREDITS_SQL = "SELECT COALESCE(sum(amount),0) FROM acc_gl_journal_entry WHERE type_enum=1"


class DockerLedger:
    """Queries the ledger through the banking stack's db-backup sidecar."""

    def __init__(self, compose_file: str, service: str = "db-backup",
                 database: str = "fineract_default", user: str = "postgres",
                 timeout: float = 60.0):
        self.compose_file = compose_file
        self.service = service
        self.database = database
        self.user = user
        self.timeout = timeout

    def _scalar(self, sql: str) -> str | None:
        argv = ["docker", "compose", "-f", self.compose_file, "exec", "-T", self.service,
                "psql", "--username", self.user, "--dbname", self.database, "-tAc", sql]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout)
        except (subprocess.SubprocessError, OSError):
            return None
        return proc.stdout.strip() if proc.returncode == 0 else None

    def row_counts(self) -> dict[str, int]:
        """Row counts per banking table. Unreadable tables are omitted, so a
        partial read is visibly partial instead of silently reported as zero."""
        counts: dict[str, int] = {}
        for table in BANKING_TABLES:
            raw = self._scalar(f"SELECT count(*) FROM {table}")
            if raw is None:
                continue
            try:
                counts[table] = int(raw)
            except ValueError:
                continue
        return counts

    def ledger_totals(self) -> tuple[float | None, float | None]:
        """Total debits and credits; None when they could not be read."""
        def as_float(raw):
            try:
                return float(raw) if raw is not None else None
            except ValueError:
                return None

        return as_float(self._scalar(DEBITS_SQL)), as_float(self._scalar(CREDITS_SQL))
