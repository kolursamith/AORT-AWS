from datetime import UTC, datetime

import pytest

from backend.security.authentication import sign, verify
from backend.security.rate_limits import PerBankRateLimiter
from backend.security.telemetry_validator import TelemetryValidator
from contracts.v1.models import RouteTelemetry
from intelligence.security_tests._targets import require_isolated_target


def test_hmac_rejects_tampering_and_stale_request() -> None:
    signature = sign("bank-1", 100, b"body", "secret")
    assert verify("bank-1", 100, b"body", signature, "secret", now=100)
    assert not verify("bank-1", 100, b"tampered", signature, "secret", now=100)
    assert not verify("bank-1", 100, b"body", signature, "secret", now=1000)


def test_per_bank_limits_are_isolated() -> None:
    limiter = PerBankRateLimiter(quota=1)
    assert limiter.check("bank-a", now=0).accepted
    assert not limiter.check("bank-a", now=0.1).accepted
    assert limiter.check("bank-b", now=0.1).accepted


def test_attack_generator_refuses_external_target() -> None:
    with pytest.raises(ValueError, match="refusing"):
        require_isolated_target("https://example.com/status")


def test_telemetry_validator_rejects_replay() -> None:
    item = RouteTelemetry(
        experiment_id="exp-1",
        route_id="route-1",
        timestamp=datetime.now(UTC),
        payment_rps=1,
        check_rps=1,
        observed_throughput_rps=2,
        p95_latency_ms=5,
        timeout_rate=0,
        queue_depth=0,
        source_id="mock-switch",
        sequence=1,
    )
    validator = TelemetryValidator({"mock-switch"})
    assert validator.validate(item) == 1.0
    with pytest.raises(ValueError, match="replayed"):
        validator.validate(item)
