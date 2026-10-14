"""HMAC request authentication for mock-bank identities."""

import hashlib
import hmac
import time


def sign(bank_id: str, timestamp: int, body: bytes, secret: str) -> str:
    message = bank_id.encode() + b"\n" + str(timestamp).encode() + b"\n" + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify(
    bank_id: str,
    timestamp: int,
    body: bytes,
    signature: str,
    secret: str,
    *,
    now: int | None = None,
    max_skew_seconds: int = 30,
) -> bool:
    current = int(time.time()) if now is None else now
    if abs(current - timestamp) > max_skew_seconds:
        return False
    expected = sign(bank_id, timestamp, body, secret)
    return hmac.compare_digest(expected, signature)
