"""Stage 03: v1.1 wire protocol, codecs, request rules and the backend decision gate.

Groups A (protocol version), B (controller requests), C (recovery decisions and fail-closed
semantics) and H (regression and isolation). Pure and deterministic: no HTTP, database, clock
or Track B code.
"""

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

import contracts.v1.models as v1
from contracts.v1.generate_schemas import SCHEMAS as V1_SCHEMAS
from contracts.v1_1 import (
    HTTP_STATUS,
    DecisionMode,
    DecisionReason,
    DecisionRequest,
    ErrorCode,
    RecoveryDecision,
    SourceSequenceMark,
    TimingPolicy,
)
from contracts.v1_1.protocol import (
    ENDPOINTS,
    ENDPOINTS_BY_NAME,
    ERROR_CODE_FOR_REFUSAL,
    FALLBACK_RULES,
    PATH_PREFIX,
    PROTOCOL_VERSION,
    RETRYABLE_CODES,
    ControllerOutcome,
    ControllerStatus,
    DispatchCandidate,
    DispatchLimits,
    GateRefusal,
    Party,
    ProtocolViolation,
    TransportFailure,
    build_fail_closed_decision,
    decode,
    encode,
    error_response,
    gate_decision,
    lagging_sources,
    outcome_for_transport_failure,
    timing_policy_digest,
    timing_policy_mismatches,
    timing_policy_reference,
    validate_decision_request,
)
from intelligence.simulation.scenarios import research_timing_policy

REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures" / "trackb_8b863da"
T0 = datetime(2026, 10, 18, 10, 0, tzinfo=UTC)
EXP = "EXP-INT-1"
ROUTE = "A1-B"
POLICY = research_timing_policy()
CONTROLLER = "trackA-controller-1"


def fixtures(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def request(**overrides) -> DecisionRequest:
    base = {
        "schema_version": "1.1",
        "experiment_id": EXP,
        "route_id": ROUTE,
        "requested_at": at(0),
        "min_telemetry_watermark": ({"source_id": "twin-probe-1", "sequence": 41},),
    }
    return DecisionRequest(**{**base, **overrides})


def decision_dict(selected=("TXN-1", "TXN-2"), versions=(4, 7), **overrides) -> dict:
    base = {
        "schema_version": "1.1",
        "experiment_id": EXP,
        "route_id": ROUTE,
        "decision_id": "DEC-1",
        "policy_version": "aorts-0.1",
        "model_version": "none",
        "timing_policy_id": POLICY.policy_id,
        "created_at": at(0),
        "expires_at": at(5),
        "decision_mode": "NORMAL",
        "budget_rps": 2,
        "selected_transaction_ids": list(selected),
        "state_versions": [
            {"transaction_id": t, "version": v} for t, v in zip(selected, versions, strict=True)
        ],
        "reason_code": "SAFE_BUDGET_SELECTED",
        "reason_detail": "test decision",
        "telemetry_watermark": [{"source_id": "twin-probe-1", "sequence": 41}],
    }
    return {**base, **overrides}


def wire(data: dict) -> bytes:
    return encode(RecoveryDecision.model_validate(data))


def candidate(txn: str, version: int, **overrides) -> DispatchCandidate:
    base = {
        "transaction_id": txn,
        "experiment_id": EXP,
        "route_id": ROUTE,
        "state_version": version,
        "terminal": False,
        "check_in_flight": False,
        "counted_check_attempts": 0,
        "eligible_from": at(-10),
        "eligible_until": at(3600),
    }
    return DispatchCandidate(**{**base, **overrides})


CANDIDATES = {"TXN-1": candidate("TXN-1", 4), "TXN-2": candidate("TXN-2", 7)}


def gate(raw, *, now=None, candidates=None, limits=None, controller=CONTROLLER, policy=POLICY,
         req=None, window=1.0):  # fmt: skip
    return gate_decision(
        raw,
        request=req or request(),
        controller_id=controller,
        authorized_controllers=frozenset({CONTROLLER}),
        timing_policy=policy,
        candidates=CANDIDATES if candidates is None else candidates,
        limits=limits or DispatchLimits(rate_allowance=100),
        now=now or at(1),
        dispatch_window_s=window,
        max_clock_skew_s=1.0,
    )


# ============================================================== A — protocol version


def test_a01_v1_contract_is_unchanged():
    for filename, model in V1_SCHEMAS.items():
        committed = json.loads((REPO / "contracts" / "v1" / filename).read_text("utf-8"))
        assert model.model_json_schema() == committed, filename
    assert v1.RecoveryDecision.model_fields["schema_version"].default == "1.0"


def test_a02_v1_and_v11_decisions_are_not_wire_compatible():
    v11 = json.loads(wire(decision_dict()))
    with pytest.raises(ValidationError):
        v1.RecoveryDecision.model_validate(v11)
    starter = fixtures("telemetry_and_checks.json")["controller"]["starter_v1_decision"]
    v1.RecoveryDecision.model_validate(starter)  # the v1 shape Track B expects today
    with pytest.raises(ProtocolViolation) as caught:
        decode(RecoveryDecision, starter)
    assert caught.value.code is ErrorCode.UNSUPPORTED_SCHEMA_VERSION


def _samples():
    policy = POLICY
    status = ControllerStatus(
        schema_version="1.1",
        protocol_version="1.1",
        service_id=CONTROLLER,
        experiment_id=EXP,
        ready=True,
        policy_version="aorts-0.1",
        model_version="none",
        timing_policy=timing_policy_reference(policy),
    )
    return [request(), RecoveryDecision.model_validate(decision_dict()), status, policy]


@pytest.mark.parametrize("message", _samples(), ids=lambda m: type(m).__name__)
def test_a03_messages_round_trip_through_the_wire(message):
    raw = encode(message)
    assert decode(type(message), raw) == message
    assert encode(decode(type(message), raw)) == raw


def test_a04_encoding_is_deterministic_and_order_independent():
    data = decision_dict()
    reordered = dict(reversed(list(data.items())))
    first = encode(RecoveryDecision.model_validate(data))
    second = encode(RecoveryDecision.model_validate(reordered))
    assert first == second
    assert first.decode().startswith('{"budget_rps":2,')
    assert b" " not in first.replace(b"test decision", b"")


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda d: d.update(surplus=1), ErrorCode.SCHEMA_INVALID),
        (lambda d: d.update(schema_version="1.0"), ErrorCode.UNSUPPORTED_SCHEMA_VERSION),
        (lambda d: d.pop("schema_version"), ErrorCode.UNSUPPORTED_SCHEMA_VERSION),
        (lambda d: d.pop("experiment_id"), ErrorCode.SCHEMA_INVALID),
        (lambda d: d.pop("route_id"), ErrorCode.SCHEMA_INVALID),
        (lambda d: d.update(route_id=""), ErrorCode.SCHEMA_INVALID),
        (lambda d: d.update(budget_rps=2.5), ErrorCode.SCHEMA_INVALID),
        (lambda d: d.update(budget_rps="2"), ErrorCode.SCHEMA_INVALID),
        (lambda d: d.update(expires_at="2026-10-18T10:00:05"), ErrorCode.SCHEMA_INVALID),
    ],
    ids=[
        "unknown-field",
        "v1-version",
        "no-version",
        "no-experiment",
        "no-route",
        "empty-route",
        "fractional-budget",
        "string-budget",
        "naive-time",
    ],  # fmt: skip
)
def test_a05_invalid_messages_are_refused_not_repaired(mutate, code):
    data = json.loads(wire(decision_dict()))
    mutate(data)
    with pytest.raises(ProtocolViolation) as caught:
        decode(RecoveryDecision, json.dumps(data))
    assert caught.value.code is code
    assert caught.value.http_status == 422


@pytest.mark.parametrize("raw", [b"not json", b"[1, 2]", b'"text"', "\udcff"])
def test_a06_non_object_bodies_are_schema_invalid(raw):
    with pytest.raises(ProtocolViolation) as caught:
        decode(DecisionRequest, raw)
    assert caught.value.code is ErrorCode.SCHEMA_INVALID


def test_a07_timestamps_are_normalised_to_utc():
    ist = timezone(timedelta(hours=5, minutes=30))
    req = request(requested_at=datetime(2026, 10, 18, 15, 30, tzinfo=ist))
    assert req.requested_at == T0 and req.requested_at.tzinfo is UTC
    assert b'"requested_at":"2026-10-18T10:00:00Z"' in encode(req)


def test_a08_endpoint_registry_is_versioned_and_complete():
    assert PROTOCOL_VERSION == "1.1" and PATH_PREFIX == "/v1.1"
    paths = [(e.method, e.path) for e in ENDPOINTS]
    assert len(paths) == len(set(paths)) == 6
    assert all(path.startswith("/v1.1/") for _, path in paths)
    assert set(ENDPOINTS_BY_NAME) == {
        "telemetry_intake",
        "transaction_event_intake",
        "status_check_result_intake",
        "recovery_decision",
        "controller_health",
        "controller_readiness",
    }
    for endpoint in ENDPOINTS:
        assert endpoint.producer is not endpoint.consumer
        assert endpoint.idempotency
        assert all(code in HTTP_STATUS for code in endpoint.error_codes)
        for model in (endpoint.request_model, endpoint.response_model):
            assert model is None or model.__module__.startswith("contracts.v1_1")
    decision = ENDPOINTS_BY_NAME["recovery_decision"]
    assert (decision.request_model, decision.response_model) == (DecisionRequest, RecoveryDecision)
    assert decision.producer is Party.TRACK_B_BACKEND


def test_a09_error_responses_follow_shared_counting_and_retry_tables():
    timeout = error_response(ErrorCode.CHECK_TIMEOUT, "no answer")
    assert (timeout.retryable, timeout.counts_against_limit) == (True, True)
    refused = error_response(ErrorCode.RATE_LIMITED, "slow down", retry_after_ms=500)
    assert (refused.retryable, refused.counts_against_limit, refused.retry_after_ms) == (
        True,
        False,
        500,
    )
    terminal = error_response(ErrorCode.SCHEMA_INVALID, "bad", retry_after_ms=500)
    assert (terminal.retryable, terminal.retry_after_ms) == (False, None)
    intake = error_response(ErrorCode.DUPLICATE_EVENT, "seen")
    assert intake.counts_against_limit is None and HTTP_STATUS[ErrorCode.DUPLICATE_EVENT] == 200
    with pytest.raises(ValueError, match="declare"):
        error_response(ErrorCode.UPSTREAM_UNAVAILABLE, "down")
    with pytest.raises(ValueError, match="fixed counting rule"):
        error_response(ErrorCode.RATE_LIMITED, "x", counts_against_limit=True)
    assert ErrorCode.EVIDENCE_REQUIRED not in RETRYABLE_CODES
    violation = ProtocolViolation(ErrorCode.EXPERIMENT_MISMATCH, "wrong experiment")
    body = violation.to_error_response(experiment_id=EXP)
    assert (violation.http_status, body.retryable, body.experiment_id) == (409, False, EXP)


# ============================================================ B — controller requests

REQUEST_RULES = {
    "experiment_id": EXP,
    "known_routes": frozenset({"A1-B", "A1-C"}),
    "max_request_age_s": 2.0,
    "max_future_skew_s": 1.0,
}


def test_b01_a_correct_route_request_is_accepted():
    raw = encode(request())
    assert validate_decision_request(raw, now=at(0.5), **REQUEST_RULES) == request()


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"experiment_id": "EXP-OTHER"}, ErrorCode.EXPERIMENT_MISMATCH),
        ({"route_id": "A9-Z"}, ErrorCode.ROUTE_IDENTITY_MISMATCH),
        ({"requested_at": at(-5)}, ErrorCode.STALE_OBSERVATION),
        ({"requested_at": at(3)}, ErrorCode.FUTURE_TIMESTAMP),
    ],
)
def test_b02_wrong_identity_or_time_is_rejected(override, code):
    with pytest.raises(ProtocolViolation) as caught:
        validate_decision_request(encode(request(**override)), now=at(0), **REQUEST_RULES)
    assert caught.value.code is code


def test_b03_request_age_boundaries_are_inclusive():
    for now in (at(2.0), at(-1.0)):
        validate_decision_request(encode(request()), now=now, **REQUEST_RULES)


def test_b04_track_b_starter_request_cannot_supply_capacity_or_candidates():
    starter = fixtures("telemetry_and_checks.json")["controller"]["starter_request"]
    with pytest.raises(ProtocolViolation) as caught:
        validate_decision_request(starter, now=at(0), **REQUEST_RULES)
    assert caught.value.code is ErrorCode.SCHEMA_INVALID
    assert "estimated_capacity_rps" in caught.value.message
    smuggled = {**json.loads(encode(request())), "estimated_capacity_rps": 900.0}
    with pytest.raises(ProtocolViolation, match="derives capacity"):
        validate_decision_request(smuggled, now=at(0), **REQUEST_RULES)
    as_bytes = json.dumps(smuggled).encode()  # the same smuggling in raw bytes
    with pytest.raises(ProtocolViolation) as caught:
        validate_decision_request(as_bytes, now=at(0), **REQUEST_RULES)
    assert caught.value.code is ErrorCode.SCHEMA_INVALID


def test_b05_extra_fields_and_duplicate_watermark_sources_are_rejected():
    data = {**json.loads(encode(request())), "priority": "high"}
    with pytest.raises(ProtocolViolation):
        validate_decision_request(data, now=at(0), **REQUEST_RULES)
    marks = [{"source_id": "p", "sequence": 1}, {"source_id": "p", "sequence": 2}]
    data = {**json.loads(encode(request())), "min_telemetry_watermark": marks}
    with pytest.raises(ProtocolViolation, match="more than once"):
        validate_decision_request(data, now=at(0), **REQUEST_RULES)


def test_b06_lagging_watermark_leads_to_an_explicit_fail_closed_decision():
    req = request(
        min_telemetry_watermark=(
            SourceSequenceMark(source_id="twin-probe-1", sequence=41),
            SourceSequenceMark(source_id="twin-probe-2", sequence=3),
        )
    )
    assert lagging_sources(req.min_telemetry_watermark, {"twin-probe-1": 41}) == (
        SourceSequenceMark(source_id="twin-probe-2", sequence=3),
    )
    assert lagging_sources(req.min_telemetry_watermark, {"twin-probe-1": 40, "twin-probe-2": 9})
    assert not lagging_sources(req.min_telemetry_watermark, {"twin-probe-1": 41, "twin-probe-2": 3})
    stale = build_fail_closed_decision(
        req,
        decision_id="DEC-STALE",
        reason=DecisionReason.TELEMETRY_STALE,
        detail="watermark twin-probe-2>=3 not reached",
        created_at=at(0),
        validity_s=5.0,
        policy_version="aorts-0.1",
        model_version="none",
        timing_policy_id=POLICY.policy_id,
    )
    assert stale.decision_mode is DecisionMode.FAIL_CLOSED
    assert (stale.budget_rps, stale.selected_transaction_ids) == (0, ())
    with pytest.raises(ValidationError):
        build_fail_closed_decision(
            req,
            decision_id="DEC-X",
            reason=DecisionReason.SAFE_BUDGET_SELECTED,
            detail="not a fail-closed reason",
            created_at=at(0),
            validity_s=5.0,
            policy_version="aorts-0.1",
            model_version="none",
            timing_policy_id=POLICY.policy_id,
        )


# ============================================================ C — recovery decisions


def test_c01_valid_decision_is_admitted_with_versions_preserved():
    plan = gate(wire(decision_dict()))
    assert plan.outcome is ControllerOutcome.DECISION and plan.decision_refusal is None
    assert plan.admitted == ("TXN-1", "TXN-2") and plan.refused == ()
    decoded = decode(RecoveryDecision, wire(decision_dict()))
    assert [(r.transaction_id, r.version) for r in decoded.state_versions] == [
        ("TXN-1", 4),
        ("TXN-2", 7),
    ]


@pytest.mark.parametrize(
    ("raw_overrides", "kwargs", "refusal"),
    [
        ({"route_id": "A1-C"}, {}, GateRefusal.ROUTE_MISMATCH),
        ({"experiment_id": "EXP-OTHER"}, {}, GateRefusal.EXPERIMENT_MISMATCH),
        ({}, {"now": at(5)}, GateRefusal.EXPIRED),
        ({}, {"now": at(-1.5)}, GateRefusal.NOT_YET_VALID),
        ({}, {"controller": "impostor"}, GateRefusal.UNAUTHORIZED_CONTROLLER),
        ({"timing_policy_id": "trackb-default"}, {}, GateRefusal.TIMING_POLICY_MISMATCH),
    ],
    ids=["route", "experiment", "expired-at-boundary", "future", "controller", "policy"],
)
def test_c02_whole_decision_refusals(raw_overrides, kwargs, refusal):
    plan = gate(wire(decision_dict(**raw_overrides)), **kwargs)
    assert plan.decision_refusal is refusal
    assert plan.admitted == () and plan.rule.controller_directed_checks == 0
    assert plan.backend_fallback_permitted


def test_c03_decision_is_valid_until_just_before_expiry():
    assert gate(wire(decision_dict()), now=at(4.999)).admitted == ("TXN-1", "TXN-2")
    assert gate(wire(decision_dict()), now=at(-1.0)).decision_refusal is None


@pytest.mark.parametrize(
    ("override", "refusal"),
    [
        ({"state_version": 5}, GateRefusal.STALE_VERSION),
        ({"terminal": True}, GateRefusal.TERMINAL),
        ({"check_in_flight": True}, GateRefusal.CHECK_IN_FLIGHT),
        ({"route_id": "A1-C"}, GateRefusal.WRONG_ROUTE),
        ({"experiment_id": "EXP-OTHER"}, GateRefusal.WRONG_EXPERIMENT),
        ({"counted_check_attempts": 3}, GateRefusal.CHECK_LIMIT_REACHED),
        ({"eligible_from": at(1.001)}, GateRefusal.NOT_YET_ELIGIBLE),
        ({"eligible_from": None}, GateRefusal.NOT_YET_ELIGIBLE),
        ({"eligible_until": at(0.999)}, GateRefusal.WINDOW_CLOSED),
    ],
)
def test_c04_per_transaction_execution_checks(override, refusal):
    candidates = {**CANDIDATES, "TXN-1": candidate("TXN-1", 4, **override)}
    plan = gate(wire(decision_dict()), candidates=candidates)
    assert plan.refused == (("TXN-1", refusal),)
    assert plan.admitted == ("TXN-2",)


def test_c05_terminal_outranks_a_matching_version_and_unknown_ids_are_refused():
    terminal_same_version = {**CANDIDATES, "TXN-1": candidate("TXN-1", 4, terminal=True)}
    assert gate(wire(decision_dict()), candidates=terminal_same_version).refused == (
        ("TXN-1", GateRefusal.TERMINAL),
    )
    plan = gate(wire(decision_dict()), candidates={"TXN-2": CANDIDATES["TXN-2"]})
    assert plan.refused == (("TXN-1", GateRefusal.UNKNOWN_TRANSACTION),)


def test_c06_eligibility_boundaries_are_exact():
    exactly = {
        **CANDIDATES,
        "TXN-1": candidate("TXN-1", 4, eligible_from=at(1), eligible_until=at(1)),
    }
    assert gate(wire(decision_dict()), candidates=exactly).admitted == ("TXN-1", "TXN-2")
    limit = POLICY.max_counted_checks - 1
    below = {**CANDIDATES, "TXN-1": candidate("TXN-1", 4, counted_check_attempts=limit)}
    assert gate(wire(decision_dict()), candidates=below).admitted == ("TXN-1", "TXN-2")


@pytest.mark.parametrize(
    ("budget", "window", "limits", "admitted", "tail"),
    [
        (1, 1.0, DispatchLimits(100), ("TXN-1",), GateRefusal.OVER_BUDGET),
        (2, 0.5, DispatchLimits(100), ("TXN-1",), GateRefusal.OVER_BUDGET),
        (2, 1.0, DispatchLimits(1), ("TXN-1",), GateRefusal.RATE_LIMITED),
        (2, 1.0, DispatchLimits(5, credit_allowance=1), ("TXN-1",), GateRefusal.CREDIT_EXHAUSTED),
        (2, 1.0, DispatchLimits(0), (), GateRefusal.RATE_LIMITED),
    ],
)
def test_c07_budget_rate_and_credit_bounds(budget, window, limits, admitted, tail):
    plan = gate(wire(decision_dict(budget_rps=budget)), limits=limits, window=window)
    assert plan.admitted == admitted
    assert plan.refused[-1] == ("TXN-2", tail)
    assert len(plan.admitted) <= min(budget * window, limits.rate_allowance)


def test_c08_selection_beyond_budget_times_validity_is_malformed_not_trimmed():
    data = decision_dict(budget_rps=0)
    with pytest.raises(ValidationError, match="exceeds budget_rps"):
        RecoveryDecision.model_validate(data)
    data["created_at"], data["expires_at"] = at(0).isoformat(), at(5).isoformat()
    plan = gate(json.dumps(data))
    assert plan.outcome is ControllerOutcome.MALFORMED_RESPONSE
    assert plan.decision_refusal is GateRefusal.MALFORMED and plan.admitted == ()


@pytest.mark.parametrize(
    "data",
    [
        decision_dict(selected=("TXN-1", "TXN-1"), versions=(4, 4)),
        {**decision_dict(), "state_versions": [{"transaction_id": "TXN-1", "version": 4}]},
        {**decision_dict(), "state_versions": []},
        {**decision_dict(), "decision_mode": "FAIL_CLOSED", "reason_code": "TELEMETRY_STALE"},
        {**decision_dict(), "reason_code": "TELEMETRY_STALE"},
        {**decision_dict(), "predicted_delay_ms": 12.0},
    ],
    ids=[
        "duplicates",
        "missing-version",
        "no-versions",
        "fail-closed-with-budget",
        "wrong-reason",
        "prediction-without-horizon",
    ],  # fmt: skip
)
def test_c09_malformed_controller_responses_are_refused_whole(data):
    raw = json.dumps(data, default=str)
    plan = gate(raw)
    assert plan.outcome is ControllerOutcome.MALFORMED_RESPONSE
    assert plan.admitted == () and plan.backend_fallback_permitted


def test_c10_explicit_fail_closed_is_not_the_backend_fixed_budget_fallback():
    stale = build_fail_closed_decision(
        request(),
        decision_id="DEC-FC",
        reason=DecisionReason.TELEMETRY_UNTRUSTED,
        detail="all sources flagged",
        created_at=at(0),
        validity_s=5.0,
        policy_version="aorts-0.1",
        model_version="none",
        timing_policy_id=POLICY.policy_id,
    )
    explicit = gate(encode(stale))
    assert explicit.outcome is ControllerOutcome.EXPLICIT_FAIL_CLOSED
    assert explicit.admitted == () and explicit.rule.controller_directed_checks == 0
    assert explicit.backend_fallback_permitted is False  # Track B must not add its 2 checks
    unavailable = outcome_for_transport_failure(TransportFailure.CONNECT_ERROR)
    assert unavailable is ControllerOutcome.CONTROLLER_UNAVAILABLE
    assert FALLBACK_RULES[unavailable].backend_fallback is True
    trackb_fallback = fixtures("telemetry_and_checks.json")["trackb_settings"]
    bad = {**json.loads(encode(stale)), "budget_rps": trackb_fallback["fallback_budget_per_route"]}
    assert gate(json.dumps(bad)).outcome is ControllerOutcome.MALFORMED_RESPONSE


def test_c11_warming_up_and_failure_classes_are_distinct():
    warming = {
        **decision_dict(selected=(), versions=()),
        "decision_mode": "WARMING_UP",
        "budget_rps": 0,
        "reason_code": "CONTROLLER_WARMING_UP",
    }
    plan = gate(wire(warming))
    assert plan.outcome is ControllerOutcome.WARMING_UP and not plan.backend_fallback_permitted
    assert outcome_for_transport_failure(TransportFailure.TIMEOUT) is (
        ControllerOutcome.CONTROLLER_TIMEOUT
    )
    assert outcome_for_transport_failure(TransportFailure.HTTP_5XX) is (
        ControllerOutcome.CONTROLLER_UNAVAILABLE
    )
    assert set(FALLBACK_RULES) == set(ControllerOutcome)
    directed = {o for o, r in FALLBACK_RULES.items() if r.controller_directed_checks != 0}
    assert directed == {ControllerOutcome.DECISION}
    no_backend = {o for o, r in FALLBACK_RULES.items() if not r.backend_fallback}
    assert no_backend == {
        ControllerOutcome.DECISION,
        ControllerOutcome.EXPLICIT_FAIL_CLOSED,
        ControllerOutcome.WARMING_UP,
    }


def test_c12_stale_versions_drop_only_the_affected_transactions():
    moved = {**CANDIDATES, "TXN-2": candidate("TXN-2", 8)}
    plan = gate(wire(decision_dict()), candidates=moved)
    assert plan.outcome is ControllerOutcome.DECISION
    assert plan.admitted == ("TXN-1",) and plan.refused == (("TXN-2", GateRefusal.STALE_VERSION),)


def test_c13_no_response_authorises_a_financial_operation():
    plan = gate(wire(decision_dict()))
    fields = set(plan.__dataclass_fields__) | set(RecoveryDecision.model_fields)
    assert not {f for f in fields if any(w in f for w in ("revers", "credit", "debit", "settle"))}
    assert {r for r in GateRefusal if r not in ERROR_CODE_FOR_REFUSAL} == {
        GateRefusal.NOT_YET_VALID,
        GateRefusal.TIMING_POLICY_MISMATCH,
        GateRefusal.TERMINAL,
        GateRefusal.CHECK_IN_FLIGHT,
        GateRefusal.WINDOW_CLOSED,
        GateRefusal.OVER_BUDGET,
    }


def test_c14_controller_status_requires_reasons_exactly_when_not_ready():
    base = json.loads(encode(_samples()[2]))
    ControllerStatus.model_validate({**base, "ready": False, "not_ready_reasons": ["warming"]})
    for bad in ({"ready": False}, {"ready": True, "not_ready_reasons": ["x"]}):
        with pytest.raises(ValidationError):
            ControllerStatus.model_validate({**base, **bad})


# ======================================================== M — timing-policy alignment


def test_m01_policies_are_referenced_by_id_and_content_digest():
    ref = timing_policy_reference(POLICY)
    assert ref.policy_id == POLICY.policy_id and len(ref.digest) == 64
    assert timing_policy_digest(POLICY) == timing_policy_digest(research_timing_policy())
    changed = POLICY.model_copy(update={"min_check_spacing_s": 60.0})
    assert timing_policy_digest(changed) != ref.digest
    assert timing_policy_mismatches(ref, POLICY) == ()
    (problem,) = timing_policy_mismatches(ref, changed)
    assert "different content" in problem
    other = TimingPolicy.model_validate({**POLICY.model_dump(), "policy_id": "trackb-default"})
    (problem,) = timing_policy_mismatches(ref, other)
    assert "policy_id differs" in problem


# ========================================================= H — regression and isolation


def test_h01_protocol_and_adapters_import_no_backend_or_network_code():
    code = (
        "import sys, contracts.v1_1.protocol, contracts.v1_1.adapters\n"
        "bad = sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'backend', 'intelligence', 'httpx', 'sqlalchemy', 'psycopg', 'fastapi', 'boto3'})\n"
        "print(','.join(bad))\n"
    )
    done = subprocess.run(  # noqa: S603 - fixed interpreter and constant code
        [sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, check=True
    )
    assert done.stdout.strip() == ""


def test_h02_gating_is_deterministic_and_does_not_mutate_inputs():
    raw = wire(decision_dict())
    candidates = dict(CANDIDATES)
    first, second = gate(raw, candidates=candidates), gate(raw, candidates=candidates)
    assert first == second and candidates == CANDIDATES
