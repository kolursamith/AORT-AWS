"""Restoring a ledger dump, and the commands that do it.

Commands are built as explicit argument lists and the target database name is
validated rather than escaped, because the name reaches a command line.
"""

from __future__ import annotations

import re
from typing import Callable, Sequence

# PostgreSQL identifiers: start with a letter or underscore, max 63 characters.
DATABASE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

Runner = Callable[[Sequence[str]], tuple[int, str, str]]


class RestoreError(RuntimeError):
    """A restore step failed."""


def _validate_database(name: str) -> str:
    if not isinstance(name, str) or not DATABASE_NAME.match(name):
        raise ValueError(f"unsafe or invalid database name: {name!r}")
    return name


def build_dropdb_argv(database: str, user: str) -> list[str]:
    # --force (PostgreSQL 13+) terminates existing sessions first. Monitoring
    # connects to every database it discovers - postgres_exporter runs with
    # auto-discovery - so without it a restore fails with "database is being
    # accessed by other users" purely because the stack is being observed.
    return ["dropdb", "--username", user, "--if-exists", "--force",
            _validate_database(database)]


def build_createdb_argv(database: str, user: str) -> list[str]:
    return ["createdb", "--username", user, _validate_database(database)]


def build_pg_restore_argv(dump_path: str, database: str, user: str) -> list[str]:
    return [
        "pg_restore",
        "--username", user,
        "--dbname", _validate_database(database),
        "--no-owner",
        "--no-privileges",
        "--exit-on-error",
        dump_path,
    ]


def compose_exec_argv(service: str, argv: Sequence[str], compose_file: str) -> list[str]:
    """Run a command inside a compose service, without allocating a TTY."""
    if not argv:
        raise ValueError("a command is required for compose exec")
    return ["docker", "compose", "-f", compose_file, "exec", "-T", service, *argv]


def restore_into(
    runner: Runner,
    dump_path: str,
    database: str,
    user: str,
    compose_file: str,
    service: str,
) -> None:
    """Drop, recreate and restore `database` from `dump_path`.

    Stops at the first failing step so a half-restored database is never
    mistaken for a successful recovery.
    """
    steps = [
        ("dropdb", build_dropdb_argv(database, user)),
        ("createdb", build_createdb_argv(database, user)),
        ("pg_restore", build_pg_restore_argv(dump_path, database, user)),
    ]
    for name, argv in steps:
        code, out, err = runner(compose_exec_argv(service, argv, compose_file))
        if code != 0:
            detail = (err or out or "").strip()
            raise RestoreError(f"{name} failed (exit {code}): {detail}")
