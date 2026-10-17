"""Twin engine: keeps the Route and Transaction Twins up to date from RECORDED events.

Sources (both are append-only tables written in the same DB transaction as the facts they record):

* ``outbox_events``          -> transaction lifecycle (DomainEvent payloads, with version);
* ``telemetry_observations`` -> validator verdicts per telemetry sample.

The engine tails both tables with id cursors (read-only: it does not interfere with the outbox
relay), so it works in any process. ``rebuild()`` replays everything from id 0 and must produce
the same state as incremental updates (tested). Switch-side measurements are snapshots, not events:
they are applied with ``observe_measurements`` and are not part of the reconstruction.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime

from sqlalchemy import select

from backend.aorts.banking.ledger.models import OutboxEvent, TelemetryObservation
from backend.aorts.twin.route_twin import RouteTwinStore
from backend.aorts.twin.transaction_twin import TransactionTwinStore, _ts


class TwinEngine:
    def __init__(self, db=None, batch: int = 5000):
        self.db = db
        self.batch = batch
        self._lock = threading.RLock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.txns = TransactionTwinStore()
            self.routes = RouteTwinStore()
            self.seen: set[str] = set()
            self.outbox_cursor = 0
            self.telemetry_cursor = 0
            self.events_processed = 0

    # ------------------------------------------------------------------ event application
    def apply_event(self, ev: dict) -> str:
        """Apply one DomainEvent dict (as stored in ``outbox_events.payload``)."""
        with self._lock:
            first_time = ev.get("event_id") not in self.seen
            outcome = self.txns.apply(ev, self.seen)
            if first_time and ev.get("event_type") == "PAYMENT_DEBITED" and ev.get("route_id"):
                at = _ts(ev.get("timestamp")) or datetime.now(UTC)
                self.routes.on_payment(ev["route_id"], at)
            self.events_processed += 1
            return outcome

    def apply_telemetry(self, route_id: str, verdict: str, reported: dict) -> None:
        with self._lock:
            self.routes.on_telemetry(route_id, verdict, reported)

    def observe_measurements(
        self, measured_by_destination: dict[str, dict], now: datetime | None = None
    ) -> None:
        now = now or datetime.now(UTC)
        with self._lock:
            for dest, m in measured_by_destination.items():
                self.routes.on_measurement(dest, m, now)

    def _refresh_backlogs(self) -> None:
        counts: dict[str, int] = {}
        for t in self.txns.twins.values():
            if t.unresolved and t.state != "DEBITED" and t.route_id:
                counts[t.route_id] = counts.get(t.route_id, 0) + 1
        for rid, rt in self.routes.routes.items():
            rt.recovery_backlog = counts.get(rid, 0)
        for rid, n in counts.items():
            self.routes.get(rid).recovery_backlog = n

    # ------------------------------------------------------------------ database tailing
    def poll(self) -> int:
        """Apply events recorded since the last poll. Returns how many rows were read."""
        if self.db is None:
            return 0
        n = 0
        with self._lock:
            with self.db.session() as s:
                while True:
                    rows = s.execute(
                        select(OutboxEvent.id, OutboxEvent.payload)
                        .where(OutboxEvent.id > self.outbox_cursor)
                        .order_by(OutboxEvent.id)
                        .limit(self.batch)
                    ).all()
                    for rid, payload in rows:
                        self.apply_event(payload)
                        self.outbox_cursor = rid
                    n += len(rows)
                    if len(rows) < self.batch:
                        break
                trows = s.execute(
                    select(
                        TelemetryObservation.id,
                        TelemetryObservation.route_id,
                        TelemetryObservation.verdict,
                        TelemetryObservation.reported,
                    )
                    .where(TelemetryObservation.id > self.telemetry_cursor)
                    .order_by(TelemetryObservation.id)
                    .limit(self.batch)
                ).all()
                for tid, route, verdict, reported in trows:
                    self.apply_telemetry(route, verdict, reported or {})
                    self.telemetry_cursor = tid
                n += len(trows)
            self._refresh_backlogs()
        return n

    def rebuild(self) -> int:
        """Reconstruct both twins from the recorded event log (from scratch)."""
        with self._lock:
            measured = {
                rid: (rt.measured, rt.measured_at) for rid, rt in self.routes.routes.items()
            }
            self.reset()
            n = self.poll()
            for rid, (
                m,
                at,
            ) in measured.items():  # measurements are snapshots: carry the latest over
                if m and at:
                    self.routes.on_measurement(self.routes.get(rid).destination, m, at)
            return n

    # ------------------------------------------------------------------ views
    def snapshot(self, now: datetime | None = None, max_txns: int = 200) -> dict:
        now = now or datetime.now(UTC)
        with self._lock:
            self._refresh_backlogs()
            unresolved = sorted(self.txns.unresolved(), key=lambda t: t.ambiguous_since or now)
            return {
                "generated_at": now.isoformat(),
                "routes": {rid: rt.as_dict(now) for rid, rt in sorted(self.routes.routes.items())},
                "transactions_unresolved": [t.as_dict() for t in unresolved[:max_txns]],
                "transactions_tracked": len(self.txns.twins),
                "unresolved_count": len(unresolved),
                "event_stats": dict(self.txns.stats),
                "cursors": {"outbox": self.outbox_cursor, "telemetry": self.telemetry_cursor},
            }

    def fingerprint(self) -> dict:
        """Order-independent summary used to compare incremental vs rebuilt state."""
        with self._lock:
            self._refresh_backlogs()
            return {
                "txns": {
                    tid: (
                        t.state,
                        t.version,
                        t.financial_outcome,
                        t.checks_started,
                        t.inconclusive_checks,
                        len(t.evidence),
                    )
                    for tid, t in self.txns.twins.items()
                },
                "routes": {
                    rid: (
                        len(rt.primary_events),
                        rt.recovery_backlog,
                        round(rt.telemetry_reliability, 6),
                        rt.telemetry_observations,
                    )
                    for rid, rt in self.routes.routes.items()
                },
            }
