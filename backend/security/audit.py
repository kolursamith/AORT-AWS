"""Structured security audit event creation (transport is handled elsewhere)."""

from datetime import UTC, datetime


def audit_event(action: str, actor: str, accepted: bool, reason: str) -> dict:
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "action": action,
        "actor": actor,
        "accepted": accepted,
        "reason": reason,
    }
