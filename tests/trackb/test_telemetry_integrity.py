"""D. Telemetry tests: validation layers, conservative fallback, poisoning experiment arms."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from backend.aorts.contracts.enums import TelemetryVerdict
from backend.aorts.contracts.requests import RouteTelemetry
from backend.aorts.security.budget_guard import BudgetGuard
from backend.aorts.security.poisoning_simulator import CASES, PoisonConfig, run_poisoning
from backend.aorts.security.telemetry_validator import TelemetryValidator

NOW = datetime(2026, 10, 14, 10, 0, 0, tzinfo=UTC)
MEASURED = {
    "latency_p50_ms": 200.0,
    "latency_p95_ms": 320.0,
    "throughput_tps": 25.0,
    "timeout_rate": 0.0,
    "backlog": 0,
    "workers": 8,
    "in_flight": 5,
    "samples": 50,
}


def obs(seq=1, **kw):
    base = dict(
        event_id=f"tel-{seq}-{kw.pop('suffix', '')}",
        source_id="twin-probe-1",
        route_id="A1-B",
        sequence=seq,
        timestamp=NOW,
        latency_p50_ms=200.0,
        latency_p95_ms=320.0,
        throughput_tps=25.0,
        timeout_rate=0.0,
        backlog=0,
        health_score=1.0,
    )
    base.update(kw)
    return RouteTelemetry(**base)


@pytest.fixture
def v():
    return TelemetryValidator(sources={"twin-probe-1"}, max_age_s=30)


def check(v, o, source="twin-probe-1", measured=MEASURED):
    return v.validate(o, authenticated_source=source, measured=measured, now=NOW)


def test_clean_observation_trusted(v):
    a = check(v, obs())
    assert a.verdict == TelemetryVerdict.TRUSTED and a.confidence >= 0.9


def test_unauthenticated_or_spoofed_source_rejected(v):
    assert check(v, obs(), source=None).verdict == TelemetryVerdict.REJECTED
    assert (
        check(v, obs(seq=2), source="twin-probe-2").verdict == TelemetryVerdict.REJECTED
    )  # claims probe-1
    v2 = TelemetryValidator(sources={"twin-probe-1"})
    assert (
        v2.validate(
            obs(source_id="rogue"), authenticated_source="rogue", measured=MEASURED, now=NOW
        ).verdict
        == TelemetryVerdict.REJECTED
    )


def test_invalid_schema_rejected():
    with pytest.raises(ValueError):
        RouteTelemetry(
            event_id="t",
            source_id="twin-probe-1",
            route_id="A1-B",
            sequence=1,
            timestamp=datetime(2026, 10, 14, 10, 0),
            latency_p50_ms=1.0,
        )  # naive timestamp
    with pytest.raises(ValueError):
        RouteTelemetry(
            event_id="t",
            source_id="twin-probe-1",
            route_id="not a route",
            sequence=1,
            timestamp=NOW,
        )


def test_stale_future_duplicate_and_out_of_order_rejected(v):
    assert check(v, obs(timestamp=NOW - timedelta(minutes=5))).verdict == TelemetryVerdict.REJECTED
    assert (
        check(v, obs(seq=2, timestamp=NOW + timedelta(minutes=5))).verdict
        == TelemetryVerdict.REJECTED
    )
    assert check(v, obs(seq=3)).verdict == TelemetryVerdict.TRUSTED
    dup = check(v, obs(seq=3))
    assert dup.verdict == TelemetryVerdict.REJECTED and any("duplicate" in r for r in dup.reasons)
    old_seq = check(v, obs(seq=2, suffix="x"))
    assert old_seq.verdict == TelemetryVerdict.REJECTED and any(
        "sequence" in r for r in old_seq.reasons
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("latency_p50_ms", -1.0),
        ("timeout_rate", 1.5),
        ("health_score", 7.0),
        ("throughput_tps", 1e9),
        ("backlog", -3),
    ],
)
def test_impossible_ranges_rejected(v, field, value):
    assert check(v, obs(**{field: value})).verdict == TelemetryVerdict.REJECTED


def test_p95_below_p50_rejected(v):
    assert (
        check(v, obs(latency_p50_ms=300.0, latency_p95_ms=100.0)).verdict
        == TelemetryVerdict.REJECTED
    )


def test_missing_values_lower_confidence(v):
    a = check(v, obs(throughput_tps=None, timeout_rate=None, backlog=None))
    assert a.confidence < 0.6 and a.verdict == TelemetryVerdict.SUSPICIOUS


def test_false_throughput_flagged_by_independent_measurement(v):
    a = check(v, obs(throughput_tps=100.0))
    assert a.verdict == TelemetryVerdict.SUSPICIOUS
    assert any("optimistic" in r for r in a.reasons)


def test_false_throughput_cannot_raise_budget():
    guard = BudgetGuard(hard_cap=50, fallback_budget=2)
    trusted = guard.apply("A1-B", 6, telemetry={"verdict": "TRUSTED"}, measured=MEASURED)
    poisoned = guard.apply("A1-B", 40, telemetry={"verdict": "SUSPICIOUS"}, measured=MEASURED)
    assert poisoned.applied <= min(2, trusted.applied)
    assert any("fallback" in r for r in poisoned.reasons)


def test_low_confidence_or_missing_telemetry_triggers_conservative_fallback():
    guard = BudgetGuard(hard_cap=50, fallback_budget=2)
    assert guard.apply("A1-C", 30, telemetry=None, measured=None).applied == 2


def test_switch_measured_headroom_caps_even_trusted_budget():
    guard = BudgetGuard(hard_cap=100, fallback_budget=2)
    saturated = {**MEASURED, "in_flight": 8, "backlog": 40}
    assert (
        guard.apply("A1-B", 60, telemetry={"verdict": "TRUSTED"}, measured=saturated).applied == 1
    )


def test_ewma_flags_sudden_uncorroborated_jump_after_warmup(v):
    for i in range(1, 8):
        check(v, obs(seq=i))
    a = v.validate(
        obs(seq=9, latency_p50_ms=200.0, latency_p95_ms=900.0),
        authenticated_source="twin-probe-1",
        measured=None,
        now=NOW,
    )
    assert any("EWMA" in r for r in a.reasons)
    assert a.verdict == TelemetryVerdict.SUSPICIOUS


def test_uncorroborated_optimistic_claim_is_not_trusted(v):
    a = check(v, obs(throughput_tps=900.0, latency_p50_ms=5.0, latency_p95_ms=8.0), measured=None)
    assert a.verdict == TelemetryVerdict.SUSPICIOUS


@pytest.mark.parametrize("case", [c for c in CASES if c != "subtle_throughput_high"])
def test_poisoning_experiment_protected_arm_bounds_harm(case):
    unprotected = run_poisoning(PoisonConfig(case=case, arm="poisoned_unprotected"))["summary"]
    protected = run_poisoning(PoisonConfig(case=case, arm="poisoned_protected"))["summary"]
    assert (
        protected["max_budget_during_attack"] <= 7
    )  # never above the trusted/headroom-derived level
    assert protected["rejected"] + protected["suspicious"] > 0  # the defense noticed
    assert protected["mean_actual_delay_ms"] <= unprotected["mean_actual_delay_ms"]


def test_clean_arm_has_no_false_positives_through_real_degradation():
    s = run_poisoning(PoisonConfig(case="throughput_high", arm="clean"))["summary"]
    assert (
        s["rejected"] == 0 and s["suspicious"] == 0
    )  # Bank B slowdown at tick 20 is learned, not flagged
    assert s["ticks_over_1000ms"] == 0


def test_subtle_poisoning_is_a_documented_limitation_but_still_bounded():
    protected = run_poisoning(
        PoisonConfig(case="subtle_throughput_high", arm="poisoned_protected")
    )["summary"]
    clean = run_poisoning(PoisonConfig(case="subtle_throughput_high", arm="clean"))["summary"]
    # +30% stays within the cross-check tolerance, so it is NOT detected (limitation)...
    assert protected["rejected"] + protected["suspicious"] == 0
    # ...but the independent switch-measured headroom cap keeps the budget at the clean level.
    assert protected["max_budget_during_attack"] <= clean["max_budget_during_attack"]
    assert protected["ticks_over_1000ms"] == 0
