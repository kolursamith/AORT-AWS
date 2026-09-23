"""Dump file naming, selection and retention.

Names are `<database>-<YYYYmmddTHHMMSSZ>.dump`, so the newest backup can be
identified from the filename alone, without trusting filesystem timestamps.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

TIME_FORMAT = "%Y%m%dT%H%M%SZ"
DUMP_PATTERN = re.compile(
    r"^(?P<database>[A-Za-z_][A-Za-z0-9_]*)-(?P<stamp>\d{8}T\d{6}Z)\.dump$"
)


def dump_name(database: str, moment: datetime) -> str:
    return f"{database}-{moment.strftime(TIME_FORMAT)}.dump"


def parse_dump_name(name: str) -> tuple[str, datetime]:
    """Split a dump filename into (database, UTC timestamp)."""
    match = DUMP_PATTERN.match(name or "")
    if not match:
        raise ValueError(f"not an AORT dump filename: {name!r}")
    try:
        moment = datetime.strptime(match["stamp"], TIME_FORMAT)
    except ValueError as exc:
        raise ValueError(f"invalid timestamp in dump filename: {name!r}") from exc
    return match["database"], moment.replace(tzinfo=timezone.utc)


def _parsed(names, database=None):
    for name in names:
        try:
            parsed_db, moment = parse_dump_name(name)
        except ValueError:
            continue  # partial (.part), notes, anything not a finished dump
        if database is None or parsed_db == database:
            yield name, moment


def newest_dump(names, database: str | None = None) -> str | None:
    """The most recent dump by filename timestamp, or None if there is none."""
    candidates = sorted(_parsed(names, database), key=lambda item: item[1])
    return candidates[-1][0] if candidates else None


def prune_candidates(names, keep: int) -> list[str]:
    """Dumps to delete so that only the newest `keep` remain, oldest first.

    Files that are not finished dumps are never returned: retention must not
    delete anything it does not understand.
    """
    if keep < 0:
        raise ValueError("keep must be >= 0")
    ordered = sorted(_parsed(names), key=lambda item: item[1])
    removable = ordered[: len(ordered) - keep] if keep else ordered
    return [name for name, _ in removable]
