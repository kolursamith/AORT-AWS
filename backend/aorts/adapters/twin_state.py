"""Versioned twin-state store with conditional (compare-and-set) writes.

Used for route-twin snapshots and other operational metadata. It is NOT used for
money: financial atomicity lives in PostgreSQL. The local implementation mirrors
DynamoDB's ``attribute_not_exists(pk) OR version = :expected`` semantics, so the
same tests run against both (see ``dynamodb_adapter.py``).
"""

from __future__ import annotations

import copy
import threading
from typing import Protocol


class VersionConflict(Exception):
    def __init__(self, key: str, expected: int | None, actual: int | None):
        super().__init__(f"version conflict on {key}: expected {expected}, actual {actual}")
        self.key, self.expected, self.actual = key, expected, actual


class VersionedStore(Protocol):
    def get(self, key: str) -> tuple[int, dict] | None: ...

    def put(self, key: str, data: dict, expected_version: int | None) -> int:
        """Write if the current version equals ``expected_version`` (None = must not exist). Returns new version."""
        ...

    def items(self, prefix: str = "") -> dict[str, tuple[int, dict]]: ...


class LocalVersionedStore:
    def __init__(self):
        self._data: dict[str, tuple[int, dict]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> tuple[int, dict] | None:
        with self._lock:
            item = self._data.get(key)
            return (item[0], copy.deepcopy(item[1])) if item else None

    def put(self, key: str, data: dict, expected_version: int | None) -> int:
        with self._lock:
            current = self._data.get(key)
            actual = current[0] if current else None
            if actual != expected_version:
                raise VersionConflict(key, expected_version, actual)
            new_version = (actual or 0) + 1
            self._data[key] = (new_version, copy.deepcopy(data))
            return new_version

    def items(self, prefix: str = "") -> dict[str, tuple[int, dict]]:
        with self._lock:
            return {
                k: (v, copy.deepcopy(d)) for k, (v, d) in self._data.items() if k.startswith(prefix)
            }


class PostgresVersionedStore:
    """Cross-process local implementation backed by the ``twin_state`` table."""

    def __init__(self, db):
        self.db = db

    def get(self, key: str) -> tuple[int, dict] | None:
        from backend.aorts.banking.ledger.models import TwinState

        with self.db.session() as s:
            row = s.get(TwinState, key)
            return (row.version, dict(row.data)) if row else None

    def put(self, key: str, data: dict, expected_version: int | None) -> int:
        from sqlalchemy import update
        from sqlalchemy.dialects.postgresql import insert

        from backend.aorts.banking.ledger.models import TwinState

        with self.db.session() as s:
            if expected_version is None:
                done = s.execute(
                    insert(TwinState)
                    .values(pk=key, version=1, data=data)
                    .on_conflict_do_nothing(index_elements=["pk"])
                    .returning(TwinState.version)
                ).scalar()
            else:
                done = s.execute(
                    update(TwinState)
                    .where(TwinState.pk == key, TwinState.version == expected_version)
                    .values(version=expected_version + 1, data=data)
                    .returning(TwinState.version)
                ).scalar()
        if done is None:
            current = self.get(key)
            raise VersionConflict(key, expected_version, current[0] if current else None)
        return done

    def items(self, prefix: str = "") -> dict[str, tuple[int, dict]]:
        from sqlalchemy import select

        from backend.aorts.banking.ledger.models import TwinState

        with self.db.session() as s:
            rows = s.execute(select(TwinState).where(TwinState.pk.like(f"{prefix}%"))).scalars()
            return {r.pk: (r.version, dict(r.data)) for r in rows}


def update_with_retry(store: VersionedStore, key: str, mutate, attempts: int = 5) -> int:
    """Read-modify-write with optimistic concurrency."""
    for _ in range(attempts):
        current = store.get(key)
        version, data = current if current else (None, {})
        try:
            return store.put(key, mutate(data), version)
        except VersionConflict:
            continue
    raise VersionConflict(key, None, None)


def make_store(settings, db=None) -> VersionedStore:
    if settings.twin_store == "dynamodb":
        from backend.aorts.adapters.dynamodb_adapter import DynamoVersionedStore

        return DynamoVersionedStore(settings.dynamodb_table, settings.aws_region)
    if settings.twin_store == "postgres" and db is not None:
        return PostgresVersionedStore(db)
    return LocalVersionedStore()
