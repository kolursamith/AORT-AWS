"""E10. Unit tests for HMAC request/response authentication and key rotation."""

from __future__ import annotations

import time

import pytest

from backend.aorts.contracts.enums import ErrorCode
from backend.aorts.security.nonce_store import NonceStore
from backend.aorts.security.request_auth import (
    AuthError,
    KeyRegistry,
    RequestVerifier,
    sign_request,
    sign_response,
    verify_response,
)

KEYS = "A1-v1=A1:aaaaaaaaaaaaaaaaaaaaaaaa,A1-v2=A1:bbbbbbbbbbbbbbbbbbbbbbbb,SWITCH-v1=SWITCH:cccccccccccccccccccccccc"


@pytest.fixture
def reg():
    return KeyRegistry.parse(KEYS)


def verifier(reg):
    return RequestVerifier(reg, NonceStore(), max_skew_seconds=300)


def test_valid_signature_and_identity(reg):
    h = sign_request(reg.get("A1-v1"), "POST", "/api/v1/pay", b'{"x":1}')
    ident = verifier(reg).verify("POST", "/api/v1/pay", "", h, b'{"x":1}')
    assert ident.identity == "A1" and "remitter_bank" in ident.roles


@pytest.mark.parametrize(
    "mutate,code",
    [
        (lambda h, b: (h, b'{"x":2}'), ErrorCode.AUTH_INVALID_SIGNATURE),  # tampered body
        (lambda h, b: ({**h, "X-AORTS-Key-Id": "Z-v1"}, b), ErrorCode.AUTH_UNKNOWN_KEY),
        (
            lambda h, b: ({k: v for k, v in h.items() if k != "X-AORTS-Nonce"}, b),
            ErrorCode.AUTH_MISSING,
        ),
        (lambda h, b: ({**h, "X-AORTS-Signature": "0" * 64}, b), ErrorCode.AUTH_INVALID_SIGNATURE),
    ],
)
def test_rejections(reg, mutate, code):
    h = sign_request(reg.get("A1-v1"), "POST", "/p", b'{"x":1}')
    h2, body = mutate(h, b'{"x":1}')
    with pytest.raises(AuthError) as e:
        verifier(reg).verify("POST", "/p", "", h2, body)
    assert e.value.code == code


def test_path_and_method_are_signed(reg):
    h = sign_request(reg.get("A1-v1"), "POST", "/api/v1/pay", b"")
    with pytest.raises(AuthError):
        verifier(reg).verify("POST", "/api/v1/check-status", "", h, b"")
    with pytest.raises(AuthError):
        verifier(reg).verify("GET", "/api/v1/pay", "", h, b"")


def test_expired_timestamp_and_repeated_nonce(reg):
    v = verifier(reg)
    old = sign_request(reg.get("A1-v1"), "POST", "/p", b"", timestamp=int(time.time()) - 3600)
    with pytest.raises(AuthError) as e:
        v.verify("POST", "/p", "", old, b"")
    assert e.value.code == ErrorCode.AUTH_EXPIRED_TIMESTAMP
    h = sign_request(reg.get("A1-v1"), "POST", "/p", b"")
    v.verify("POST", "/p", "", h, b"")
    with pytest.raises(AuthError) as e:
        v.verify("POST", "/p", "", h, b"")
    assert e.value.code == ErrorCode.AUTH_REPLAYED_NONCE


def test_bad_signature_does_not_burn_nonce(reg):
    v = verifier(reg)
    h = sign_request(reg.get("A1-v1"), "POST", "/p", b"")
    with pytest.raises(AuthError):
        v.verify("POST", "/p", "", {**h, "X-AORTS-Signature": "f" * 64}, b"")
    v.verify("POST", "/p", "", h, b"")  # the genuine request with that nonce still works


def test_key_rotation_both_versions_valid_and_signing_pin(reg):
    v = verifier(reg)
    for kid in ("A1-v1", "A1-v2"):
        v.verify("POST", "/p", "", sign_request(reg.get(kid), "POST", "/p", b""), b"")
    assert reg.signing_key_for("A1").key_id == "A1-v2"  # highest version by default
    pinned = KeyRegistry.parse(KEYS, {"A1": "A1-v1"})
    assert pinned.signing_key_for("A1").key_id == "A1-v1"


def test_short_secret_rejected():
    with pytest.raises(ValueError):
        KeyRegistry.parse("A1-v1=A1:short")


def test_response_signature_binds_identity_status_and_body(reg):
    body = b'{"status":"FAILED"}'
    h = sign_response(reg.get("SWITCH-v1"), 200, "/api/v1/check-status", body)
    assert verify_response(reg, "SWITCH", 200, "/api/v1/check-status", h, body)
    assert not verify_response(
        reg, "SWITCH", 200, "/api/v1/check-status", h, b'{"status":"SUCCESS"}'
    )
    assert not verify_response(reg, "SWITCH", 500, "/api/v1/check-status", h, body)
    forged = sign_response(reg.get("A1-v1"), 200, "/api/v1/check-status", body)  # wrong signer
    assert not verify_response(reg, "SWITCH", 200, "/api/v1/check-status", forged, body)
