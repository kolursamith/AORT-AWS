"""Structured, hash-chained audit log.

* Audit rows tied to a financial change are written in the SAME database
  transaction as that change, so they cannot disappear on a crash after commit.
* Each row stores ``hash = SHA256(prev_hash || canonical_json(row))``. A chain is
  verified with :func:`verify_chain`. This is tamper-*evident* only: a privileged
  attacker who controls the whole database can rewrite every row and recompute
  every hash unless the head hash is anchored somewhere independent (e.g. an S3
  Object-Lock bucket). See SECURITY.md.
* High-volume security decisions (e.g. thousands of rate-limit rejections per
  second during a flood) go through :class:`BufferedAuditWriter`, which batches
  inserts and keeps exact counters; it samples individual rows per (actor, reason)
  per second so a flood cannot turn the audit log into a second DoS vector.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import defaultdict
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from backend.aorts.app.logging_config import log_event, redact
from backend.aorts.banking.ledger.models import AuditEvent
from backend.aorts.contracts.events import new_id

logger = logging.getLogger("aorts.audit")
GENESIS = "0" * 64
_AUDIT_LOCK_KEY = 7_420_001  # pg_advisory_xact_lock key serialising chain appends


def _canonical(fields: dict) -> bytes:
    return json.dumps(fields, sort_keys=True, separators=(",", ":"), default=str).encode()


def _hash_fields(row: dict) -> dict:
    return {
        "event_id": row["event_id"],
        "ts": row["ts"].astimezone(UTC).isoformat(),
        "experiment_id": row.get("experiment_id"),
        "correlation_id": row.get("correlation_id"),
        "actor": row["actor"],
        "transaction_id": row.get("transaction_id"),
        "route_id": row.get("route_id"),
        "action": row["action"],
        "decision": row["decision"],
        "reason": row.get("reason"),
        "previous_version": row.get("previous_version"),
        "new_version": row.get("new_version"),
        "metadata": row.get("metadata") or {},
    }


def compute_hash(prev_hash: str, row: dict) -> str:
    return hashlib.sha256(prev_hash.encode() + _canonical(_hash_fields(row))).hexdigest()


def make_record(
    action: str,
    decision: str,
    actor: str,
    *,
    reason: str | None = None,
    transaction_id: str | None = None,
    route_id: str | None = None,
    experiment_id: str | None = None,
    correlation_id: str | None = None,
    previous_version: int | None = None,
    new_version: int | None = None,
    metadata: dict | None = None,
) -> dict:
    return {
        "event_id": new_id("aud"),
        "ts": datetime.now(UTC),
        "experiment_id": experiment_id,
        "correlation_id": correlation_id,
        "actor": actor,
        "transaction_id": transaction_id,
        "route_id": route_id,
        "action": action,
        "decision": decision,
        "reason": (reason or None) and str(reason)[:300],
        "previous_version": previous_version,
        "new_version": new_version,
        "metadata": redact(metadata or {}),
    }


def append_records(session: Session, records: list[dict]) -> None:
    """Append records to the chain inside the caller's transaction."""
    if not records:
        return
    session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _AUDIT_LOCK_KEY})
    prev = session.execute(
        select(AuditEvent.hash).order_by(AuditEvent.seq.desc()).limit(1)
    ).scalar()
    prev = prev or GENESIS
    for row in records:
        digest = compute_hash(prev, row)
        session.add(
            AuditEvent(
                event_id=row["event_id"],
                ts=row["ts"],
                experiment_id=row["experiment_id"],
                correlation_id=row["correlation_id"],
                actor=row["actor"],
                transaction_id=row["transaction_id"],
                route_id=row["route_id"],
                action=row["action"],
                decision=row["decision"],
                reason=row["reason"],
                previous_version=row["previous_version"],
                new_version=row["new_version"],
                metadata_json=row["metadata"],
                prev_hash=prev,
                hash=digest,
            )
        )
        log_event(
            logger,
            "audit",
            action=row["action"],
            decision=row["decision"],
            actor=row["actor"],
            transaction_id=row["transaction_id"],
            route_id=row["route_id"],
            reason=row["reason"],
        )
        prev = digest
    session.flush()


def verify_chain(session: Session, limit: int | None = None) -> dict:
    """Recompute every hash. Returns {'ok': bool, 'checked': n, 'first_bad_seq': seq|None}."""
    prev = GENESIS
    checked = 0
    stmt = select(AuditEvent).order_by(AuditEvent.seq)
    if limit:
        stmt = stmt.limit(limit)
    for row in session.execute(stmt).scalars():
        fields = {
            "event_id": row.event_id,
            "ts": row.ts,
            "experiment_id": row.experiment_id,
            "correlation_id": row.correlation_id,
            "actor": row.actor,
            "transaction_id": row.transaction_id,
            "route_id": row.route_id,
            "action": row.action,
            "decision": row.decision,
            "reason": row.reason,
            "previous_version": row.previous_version,
            "new_version": row.new_version,
            "metadata": row.metadata_json,
        }
        if row.prev_hash != prev or compute_hash(prev, fields) != row.hash:
            return {"ok": False, "checked": checked, "first_bad_seq": row.seq}
        prev = row.hash
        checked += 1
    return {"ok": True, "checked": checked, "first_bad_seq": None, "head_hash": prev}


class BufferedAuditWriter:
    """Batches high-volume security audit rows; keeps exact per-reason counters."""

    def __init__(self, db, flush_interval: float = 0.5, sample_per_key_per_sec: int = 5):
        self.db = db
        self.flush_interval = flush_interval
        self.sample = sample_per_key_per_sec
        self._buf: list[dict] = []
        self._lock = threading.Lock()
        self._window: dict[tuple, int] = defaultdict(int)
        self._window_start = time.monotonic()
        self.counters: dict[str, int] = defaultdict(int)
        self.suppressed = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def record(self, rec: dict, *, sample_key: tuple | None = None) -> None:
        with self._lock:
            self.counters[f"{rec['action']}:{rec['decision']}:{rec.get('reason')}"] += 1
            now = time.monotonic()
            if now - self._window_start >= 1.0:
                self._window.clear()
                self._window_start = now
            if sample_key is not None:
                self._window[sample_key] += 1
                if self._window[sample_key] > self.sample:
                    self.suppressed += 1
                    return
            self._buf.append(rec)

    def flush(self) -> int:
        with self._lock:
            batch, self._buf = self._buf, []
        if not batch:
            return 0
        try:
            with self.db.session() as s:
                append_records(s, batch)
        except Exception:  # noqa: BLE001 - audit flush must never crash the service
            logger.exception("audit flush failed; re-queueing %d rows", len(batch))
            with self._lock:
                self._buf = batch + self._buf
            return 0
        return len(batch)

    def start(self) -> None:
        if self._thread:
            return

        def _loop():
            while not self._stop.wait(self.flush_interval):
                self.flush()

        self._thread = threading.Thread(target=_loop, name="audit-flush", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.flush()
