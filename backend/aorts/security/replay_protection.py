"""Replay / staleness checks for signed transaction-state events (Security Module 3).

Layers, outermost first:

1. HMAC signature + timestamp window + nonce (``request_auth.py``): a byte-identical
   replayed HTTP request is rejected before the body is parsed.
2. Event freshness: the event's own ``timestamp`` must be recent.
3. Linkage: transaction, bank, route and amount must match the authoritative row.
4. Event-id uniqueness (``processed_events`` primary key, in the same DB transaction).
5. Version check against the locked transaction row (stale versions rejected).
6. Financial-operation uniqueness constraints (the last line of defence).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from backend.aorts.banking.ledger.invariants import SafetyViolation
from backend.aorts.contracts.enums import ErrorCode, SwitchStatus
from backend.aorts.contracts.requests import StatusEvidence, TransactionStateEvent

ACCEPTED_EVENT_TYPES = {"STATUS_RESPONSE"}


def validate_state_event(
    event: TransactionStateEvent,
    txn,
    *,
    signer: str,
    max_age_seconds: int = 300,
    max_future_seconds: int = 5,
    now: datetime | None = None,
) -> StatusEvidence:
    now = now or datetime.now(UTC)
    if event.event_type not in ACCEPTED_EVENT_TYPES:
        raise SafetyViolation(
            ErrorCode.VALIDATION_ERROR, f"unsupported event_type {event.event_type}", 422
        )
    ts = event.timestamp if event.timestamp.tzinfo else event.timestamp.replace(tzinfo=UTC)
    if ts < now - timedelta(seconds=max_age_seconds):
        raise SafetyViolation(
            ErrorCode.STALE_VERSION,
            "event timestamp too old (stale or replayed event)",
            409,
            {"event_timestamp": ts.isoformat()},
        )
    if ts > now + timedelta(seconds=max_future_seconds):
        raise SafetyViolation(ErrorCode.VALIDATION_ERROR, "event timestamp is in the future", 422)
    if txn is None:
        raise SafetyViolation(ErrorCode.TRANSACTION_NOT_FOUND, "transaction not found", 404)
    mismatches = [
        name
        for name, a, b in (
            ("bank_id", event.bank_id, txn.remitter_bank_id),
            ("route_id", event.route_id, txn.route_id),
            ("amount_paise", event.amount_paise, txn.amount_paise),
        )
        if a != b
    ]
    if mismatches:
        raise SafetyViolation(
            ErrorCode.WRONG_TRANSACTION,
            f"event does not match transaction: {mismatches}",
            409,
            {"mismatched_fields": mismatches},
        )
    try:
        status = SwitchStatus(str(event.payload.get("status")))
    except ValueError as exc:
        raise SafetyViolation(
            ErrorCode.VALIDATION_ERROR, "payload.status must be SUCCESS|FAILED|PENDING", 422
        ) from exc
    return StatusEvidence(
        transaction_id=event.transaction_id,
        status=status,
        source=signer,
        switch_reference=event.payload.get("switch_reference"),
        observed_at=ts,
        authenticated=True,
        reason=event.payload.get("reason"),
    )
