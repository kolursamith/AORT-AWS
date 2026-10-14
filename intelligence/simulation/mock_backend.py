"""Controller-independent response source with hidden synthetic ground truth."""

import hashlib


def hidden_outcome(transaction_id: str) -> str:
    """Return reproducible simulator truth; never expose it as observed telemetry."""
    digest = hashlib.sha256(transaction_id.encode()).digest()
    return "credited" if digest[0] % 2 == 0 else "not_credited"


def dummy_observable_response(transaction_id: str) -> dict[str, str | bool]:
    return {
        "transaction_id": transaction_id,
        "outcome": "unknown",
        "authoritative": False,
        "reason": "mock adapter has no validated beneficiary evidence",
    }
