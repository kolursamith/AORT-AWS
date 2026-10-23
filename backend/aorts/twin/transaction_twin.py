"""Transaction Twin: Track B's model of every transaction that became (or may become) ambiguous.

It is a *projection* of recorded events, never an authority: money and state transitions are
decided only by the PostgreSQL ledger. The twin exists to help the recovery controller choose
which ambiguous transactions to check and when.

Event-ordering rules (each lifecycle event carries the transaction ``version`` it produced and the
full ``last_known_state``, so last-writer-wins by version is correct):

* duplicate ``event_id``             -> ignored (counted as ``duplicates``);
* ``version`` < current version      -> stale/delayed: state NOT changed (counted as ``stale``);
* ``version`` == current, other id   -> conflicting duplicate: ignored (counted as ``conflicts``);
* ``version`` > current + 1          -> out-of-order (a gap): applied, gap counted (``gaps``);
  any older event that arrives later is then simply stale.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime

UNRESOLVED = {"DEBITED", "AMBIGUOUS", "CHECK_IN_FLIGHT"}
EVIDENCE_EVENTS = {
    "PAYMENT_SUCCEEDED",
    "PAYMENT_FAILED",
    "CHECK_RESULT",
    "REVERSAL_COMPLETED",
    "PAYMENT_CLOSED",
}


def _ts(value) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


@dataclass
class TransactionTwinState:
    transaction_id: str
    route_id: str | None = None
    bank_id: str | None = None
    amount_paise: int | None = None
    state: str = "UNKNOWN"
    financial_outcome: str = "UNKNOWN"
    version: int = 0
    created_at: datetime | None = None
    ambiguous_since: datetime | None = None
    deadline_at: datetime | None = None
    last_event_at: datetime | None = None
    checks_started: int = 0
    inconclusive_checks: int = 0
    last_check_at: datetime | None = None
    evidence: list[dict] = field(default_factory=list)  # authoritative statements seen so far
    events_applied: int = 0

    @property
    def unresolved(self) -> bool:
        return self.state in UNRESOLVED

    def as_dict(self) -> dict:
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, datetime):
                d[k] = v.isoformat()
        d["unresolved"] = self.unresolved
        return d


class TransactionTwinStore:
    def __init__(self, max_evidence: int = 10):
        self.twins: dict[str, TransactionTwinState] = {}
        self.max_evidence = max_evidence
        self.stats = {"applied": 0, "duplicates": 0, "stale": 0, "conflicts": 0, "gaps": 0}

    def apply(self, ev: dict, seen: set[str]) -> str:
        """Apply one lifecycle DomainEvent (dict). Returns the outcome label."""
        eid, tid = ev.get("event_id"), ev.get("transaction_id")
        if not tid:
            return "ignored"
        if eid in seen:
            self.stats["duplicates"] += 1
            return "duplicate"
        seen.add(eid)
        version = int(ev.get("transaction_version") or 0)
        twin = self.twins.get(tid)
        if twin is None:
            twin = self.twins[tid] = TransactionTwinState(transaction_id=tid)
        payload = ev.get("payload") or {}
        at = _ts(ev.get("timestamp"))
        # Static facts are safe to fill from any event, even a stale one.
        twin.route_id = twin.route_id or ev.get("route_id")
        twin.bank_id = twin.bank_id or ev.get("bank_id")
        twin.amount_paise = twin.amount_paise or ev.get("amount_paise")
        twin.deadline_at = twin.deadline_at or _ts(payload.get("deadline_at"))
        twin.created_at = twin.created_at or _ts(payload.get("created_at"))
        if version < twin.version:
            self.stats["stale"] += 1
            return "stale"
        if version == twin.version and twin.events_applied:
            self.stats["conflicts"] += 1
            return "conflict"
        if twin.version and version > twin.version + 1:
            self.stats["gaps"] += 1
        twin.version = version
        twin.state = payload.get("last_known_state") or twin.state
        twin.financial_outcome = payload.get("financial_outcome") or twin.financial_outcome
        twin.ambiguous_since = _ts(payload.get("ambiguous_since")) or twin.ambiguous_since
        if twin.state == "AMBIGUOUS" and twin.ambiguous_since is None:
            twin.ambiguous_since = at
        twin.last_event_at = at
        etype = ev.get("event_type")
        if etype == "CHECK_STARTED":
            twin.checks_started += 1
            twin.last_check_at = at
        elif etype == "CHECK_RESULT" and twin.state == "AMBIGUOUS":
            twin.inconclusive_checks += 1
        if etype in EVIDENCE_EVENTS and payload.get("financial_outcome") not in (None, "UNKNOWN"):
            twin.evidence.append(
                {
                    "event_type": etype,
                    "outcome": payload.get("financial_outcome"),
                    "source": payload.get("evidence_source"),
                    "at": ev.get("timestamp"),
                    "version": version,
                }
            )
            del twin.evidence[: -self.max_evidence]
        twin.events_applied += 1
        self.stats["applied"] += 1
        return "applied"

    def unresolved(self, route_id: str | None = None) -> list[TransactionTwinState]:
        return [
            t
            for t in self.twins.values()
            if t.unresolved and (route_id is None or t.route_id == route_id)
        ]
