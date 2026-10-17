"""Runtime configuration shared by all services through the ``runtime_config`` table.

Fault injection and defense toggles are written by the control service (admin
only) and read by every service with a ~1 s cache, so a change made from the
dashboard reaches the switch and beneficiary containers without restarts.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from backend.aorts.banking.ledger.models import RuntimeConfig
from backend.aorts.contracts.requests import DefenseConfig, FaultConfig

DEFENSES_KEY = "defenses"
FAULT_PREFIX = "fault:"


class RuntimeConfigStore:
    def __init__(self, db, ttl_seconds: float = 1.0):
        self.db = db
        self.ttl = ttl_seconds
        self._cache: dict[str, dict] = {}
        self._loaded_at = 0.0
        self._lock = threading.Lock()

    def _refresh(self, force: bool = False) -> dict[str, dict]:
        with self._lock:
            if force or time.monotonic() - self._loaded_at > self.ttl:
                with self.db.session() as s:
                    self._cache = {
                        r.key: r.value for r in s.execute(select(RuntimeConfig)).scalars()
                    }
                self._loaded_at = time.monotonic()
            return self._cache

    def get(self, key: str, default: dict | None = None) -> dict | None:
        return self._refresh().get(key, default)

    def put(self, key: str, value: dict, actor: str) -> None:
        with self.db.session() as s:
            stmt = insert(RuntimeConfig).values(
                key=key, value=value, updated_by=actor, updated_at=datetime.now(UTC)
            )
            s.execute(
                stmt.on_conflict_do_update(
                    index_elements=["key"],
                    set_={"value": value, "updated_by": actor, "updated_at": datetime.now(UTC)},
                )
            )
        self._refresh(force=True)

    def delete_prefix(self, prefix: str) -> list[str]:
        with self.db.session() as s:
            rows = (
                s.execute(select(RuntimeConfig).where(RuntimeConfig.key.like(f"{prefix}%")))
                .scalars()
                .all()
            )
            keys = [r.key for r in rows]
            for r in rows:
                s.delete(r)
        self._refresh(force=True)
        return keys

    # Typed helpers ---------------------------------------------------------------
    def defenses(self) -> DefenseConfig:
        raw = self.get(DEFENSES_KEY)
        return DefenseConfig(**raw) if raw else DefenseConfig()

    def set_defenses(self, cfg: DefenseConfig, actor: str) -> None:
        self.put(DEFENSES_KEY, cfg.model_dump(), actor)

    def fault(self, target: str) -> FaultConfig | None:
        raw = self.get(FAULT_PREFIX + target)
        return FaultConfig(**raw) if raw else None

    def set_fault(self, cfg: FaultConfig, actor: str) -> None:
        self.put(FAULT_PREFIX + cfg.target, cfg.model_dump(), actor)

    def faults(self) -> dict[str, dict]:
        return {
            k[len(FAULT_PREFIX) :]: v
            for k, v in self._refresh().items()
            if k.startswith(FAULT_PREFIX)
        }

    def reset_faults(self) -> list[str]:
        return self.delete_prefix(FAULT_PREFIX)
