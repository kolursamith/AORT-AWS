"""HMAC-SHA256 service-to-service request signing (teaching prototype).

Signed message (UTF-8, newline separated)::

    AORTS-HMAC-SHA256
    <HTTP METHOD>
    <path, without query string>
    <query string, may be empty>
    <unix timestamp, integer seconds>
    <nonce>
    <hex SHA-256 of the exact request body bytes>

Headers: ``X-AORTS-Key-Id``, ``X-AORTS-Timestamp``, ``X-AORTS-Nonce``,
``X-AORTS-Signature`` (hex). Verification uses ``hmac.compare_digest``.

Key ids are versioned (``A1-v1``, ``A1-v2``) so a key can be rotated by adding
the new key, switching ``AORTS_SIGNING_KEYS`` and later removing the old one.
This is NOT a complete enterprise identity system (no PKI, no mTLS, shared
symmetric secrets); see SECURITY.md.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass

from backend.aorts.contracts.enums import ErrorCode
from backend.aorts.security.nonce_store import NonceStore

ALGORITHM = "AORTS-HMAC-SHA256"
H_KEY_ID = "X-AORTS-Key-Id"
H_TIMESTAMP = "X-AORTS-Timestamp"
H_NONCE = "X-AORTS-Nonce"
H_SIGNATURE = "X-AORTS-Signature"
H_RESPONSE_SIGNATURE = "X-AORTS-Response-Signature"
H_RESPONSE_KEY_ID = "X-AORTS-Response-Key-Id"
H_RESPONSE_TIMESTAMP = "X-AORTS-Response-Timestamp"

# Roles granted to each synthetic identity. Attack/admin permissions are separate.
IDENTITY_ROLES: dict[str, set[str]] = {
    "A1": {"remitter_bank"},
    "A2": {"remitter_bank"},
    "A3": {"remitter_bank"},
    "SWITCH": {"switch"},
    "B": {"beneficiary_bank"},
    "C": {"beneficiary_bank"},
    "REMITTER": {"remitter_service"},
    "CHANNEL": {"payment_channel"},  # customer channel / load generator submitting payments
    "DISPATCHER": {"dispatcher"},
    "CONTROLLER": {"controller"},
    "twin-probe-1": {"telemetry_source"},
    "twin-probe-2": {"telemetry_source"},
    "LAB": {"attack_lab"},
}


@dataclass(frozen=True)
class ServiceKey:
    key_id: str
    identity: str
    secret: bytes


@dataclass(frozen=True)
class VerifiedIdentity:
    identity: str
    key_id: str
    roles: frozenset[str]


class AuthError(Exception):
    def __init__(self, code: ErrorCode, message: str, status: int = 401):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class KeyRegistry:
    def __init__(self, keys: dict[str, ServiceKey], signing: dict[str, str] | None = None):
        self._keys = keys
        self._signing = signing or {}

    @classmethod
    def parse(cls, raw: str, signing: dict[str, str] | None = None) -> KeyRegistry:
        """Parse ``key_id=identity:secret,...``. Secrets must be >= 16 chars."""
        keys: dict[str, ServiceKey] = {}
        for item in raw.split(","):
            item = item.strip()
            if not item:
                continue
            key_id, _, rest = item.partition("=")
            identity, _, secret = rest.partition(":")
            if not key_id or not identity or len(secret) < 16:
                raise ValueError(
                    f"malformed service key entry for key id '{key_id}' (secret must be >= 16 chars)"
                )
            keys[key_id.strip()] = ServiceKey(
                key_id.strip(), identity.strip(), secret.strip().encode()
            )
        return cls(keys, signing)

    def get(self, key_id: str) -> ServiceKey | None:
        return self._keys.get(key_id)

    def signing_key_for(self, identity: str) -> ServiceKey:
        key_id = self._signing.get(identity)
        if key_id and key_id in self._keys:
            return self._keys[key_id]
        candidates = sorted(k for k, v in self._keys.items() if v.identity == identity)
        if not candidates:
            raise KeyError(f"no signing key configured for identity '{identity}'")
        return self._keys[candidates[-1]]  # highest version wins by default

    def identities(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for key in self._keys.values():
            out.setdefault(key.identity, []).append(key.key_id)
        return out


def body_digest(body: bytes) -> str:
    return hashlib.sha256(body or b"").hexdigest()


def canonical_message(
    method: str, path: str, query: str, timestamp: str, nonce: str, body: bytes
) -> bytes:
    return "\n".join(
        [ALGORITHM, method.upper(), path, query, timestamp, nonce, body_digest(body)]
    ).encode()


def sign_request(
    key: ServiceKey,
    method: str,
    path: str,
    body: bytes,
    query: str = "",
    timestamp: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    ts = str(int(timestamp if timestamp is not None else time.time()))
    nonce = nonce or secrets.token_hex(16)
    sig = hmac.new(
        key.secret, canonical_message(method, path, query, ts, nonce, body), hashlib.sha256
    ).hexdigest()
    return {H_KEY_ID: key.key_id, H_TIMESTAMP: ts, H_NONCE: nonce, H_SIGNATURE: sig}


class RequestVerifier:
    def __init__(self, registry: KeyRegistry, nonces: NonceStore, max_skew_seconds: int = 300):
        self.registry = registry
        self.nonces = nonces
        self.max_skew = max_skew_seconds

    def verify(
        self, method: str, path: str, query: str, headers, body: bytes, now: float | None = None
    ) -> VerifiedIdentity:
        key_id = headers.get(H_KEY_ID)
        ts = headers.get(H_TIMESTAMP)
        nonce = headers.get(H_NONCE)
        sig = headers.get(H_SIGNATURE)
        if not (key_id and ts and nonce and sig):
            raise AuthError(ErrorCode.AUTH_MISSING, "missing authentication headers")
        key = self.registry.get(key_id)
        if key is None:
            raise AuthError(ErrorCode.AUTH_UNKNOWN_KEY, "unknown key id")
        if not ts.isdigit() or len(nonce) < 16 or len(nonce) > 128:
            raise AuthError(ErrorCode.AUTH_INVALID_SIGNATURE, "malformed timestamp or nonce")
        expected = hmac.new(
            key.secret, canonical_message(method, path, query, ts, nonce, body), hashlib.sha256
        ).hexdigest()
        # Verify the signature BEFORE touching the nonce store so unauthenticated
        # traffic cannot burn other identities' nonces.
        if not hmac.compare_digest(expected, sig):
            raise AuthError(ErrorCode.AUTH_INVALID_SIGNATURE, "signature mismatch")
        current = now if now is not None else time.time()
        if abs(current - int(ts)) > self.max_skew:
            raise AuthError(
                ErrorCode.AUTH_EXPIRED_TIMESTAMP, "request timestamp outside allowed window"
            )
        if not self.nonces.check_and_store(key_id, nonce):
            raise AuthError(ErrorCode.AUTH_REPLAYED_NONCE, "nonce already used")
        return VerifiedIdentity(
            key.identity, key_id, frozenset(IDENTITY_ROLES.get(key.identity, set()))
        )


# --- response signing (lets the remitter authenticate the *source* of evidence) --------


def _response_message(status_code: int, path: str, timestamp: str, body: bytes) -> bytes:
    return "\n".join(
        [ALGORITHM + "-RESPONSE", str(status_code), path, timestamp, body_digest(body)]
    ).encode()


def sign_response(key: ServiceKey, status_code: int, path: str, body: bytes) -> dict[str, str]:
    ts = str(int(time.time()))
    sig = hmac.new(
        key.secret, _response_message(status_code, path, ts, body), hashlib.sha256
    ).hexdigest()
    return {H_RESPONSE_KEY_ID: key.key_id, H_RESPONSE_TIMESTAMP: ts, H_RESPONSE_SIGNATURE: sig}


def verify_response(
    registry: KeyRegistry,
    expected_identity: str,
    status_code: int,
    path: str,
    headers,
    body: bytes,
    max_skew: int = 300,
) -> bool:
    key_id = headers.get(H_RESPONSE_KEY_ID)
    ts = headers.get(H_RESPONSE_TIMESTAMP)
    sig = headers.get(H_RESPONSE_SIGNATURE)
    if not (key_id and ts and sig and ts.isdigit()):
        return False
    key = registry.get(key_id)
    if key is None or key.identity != expected_identity:
        return False
    if abs(time.time() - int(ts)) > max_skew:
        return False
    expected = hmac.new(
        key.secret, _response_message(status_code, path, ts, body), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, sig)


def fingerprint(signature: str | None) -> str | None:
    """Short non-reversible fingerprint for logs: never log full signatures."""
    if not signature:
        return None
    return hashlib.sha256(signature.encode()).hexdigest()[:10]
