"""Stage 03: pure Track B -> v1.1 adapters (groups D events, E telemetry, F status, G fixtures).

Fixtures are synthetic records in the exact shapes Track B produces at feature/backend@8b863da.
``boundary.json`` holds only the *additional* v1.1 metadata Track B does not emit yet; every
test that uses it says so, and the same records without it are rejected.
"""

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from contracts.v1_1 import (
    CheckResultKind,
    ErrorCode,
    ErrorResponse,
    FinancialOutcome,
    LedgerState,
    RouteDescriptor,
    StatusCheckResult,
    TransactionEventType,
    ValidatedRouteTelemetry,
)
from contracts.v1_1.adapters import (
    EVENT_BOUNDARY_FIELDS,
    OUTBOX_RULES,
    TELEMETRY_BOUNDARY_FIELDS,
    TRACKB_CHECK_RESULT,
    TRACKB_OUTCOME,
    Adapted,
    Incompatible,
    MappingStatus,
    adapt_outbox_event,
    adapt_status_check,
    adapt_validated_telemetry,
    express_trackb_timing_policy,
    find_outbox_rule,
)
from contracts.v1_1.protocol import encode, timing_policy_mismatches, timing_policy_reference
from intelligence.common import ManualClock
from intelligence.simulation.scenarios import research_timing_policy
from intelligence.twin import (
    ApplyStatus,
    IngestStatus,
    RouteTwinConfig,
    RouteTwinRegistry,
    TransactionTwinRegistry,
)

FIXTURES = Path(__file__).parent / "fixtures" / "trackb_8b863da"
EVENTS = json.loads((FIXTURES / "outbox_events.json").read_text("utf-8"))["events"]
BOUNDARY = json.loads((FIXTURES / "boundary.json").read_text("utf-8"))
OTHER = json.loads((FIXTURES / "telemetry_and_checks.json").read_text("utf-8"))
TELEMETRY, CHECKS = OTHER["telemetry"], OTHER["status_checks"]
POLICY = research_timing_policy().model_copy(update={"policy_id": "research-60min"})

REPRESENTABLE = (
    "debit",
    "timeout",
    "check_started",
    "check_inconclusive",
    "check_started_2",
    "succeeded",
    "closed",
    "failed",
    "reversal_completed",
    "not_admitted",
    "escalated",
)
NOT_REPRESENTABLE = ("reversal_initiated", "reconciled", "quarantined")


def event(name: str) -> dict:
    record = copy.deepcopy(EVENTS[name])
    record.pop("_source")
    return record


def event_boundary(name: str, **overrides) -> dict:
    """ADDITIONAL metadata (boundary.json); Track B does not emit it at 8b863da."""

    return {
        **BOUNDARY["events"]["common"],
        **copy.deepcopy(BOUNDARY["events"]["per_event"][name]),
        **overrides,
    }


def adapt(name: str, **overrides):
    return adapt_outbox_event(event(name), event_boundary(name, **overrides))


def ok(result) -> object:
    assert isinstance(result, Adapted), result
    return result.value


# ================================================================ D — transaction events


def test_d01_every_fixture_event_has_exactly_one_rule():
    assert set(EVENTS) == set(REPRESENTABLE) | set(NOT_REPRESENTABLE)
    for name in EVENTS:
        rule = find_outbox_rule(event(name))
        assert rule is not None, name
        matching = [r for r in OUTBOX_RULES if r is rule]
        assert len(matching) == 1
    keys = [(r.trackb_event_type, r.new_state, tuple(sorted(map(str, r.previous_states))))
            for r in OUTBOX_RULES]  # fmt: skip
    assert len(keys) == len(set(keys))


@pytest.mark.parametrize("name", sorted(EVENTS))
def test_d02_real_track_b_events_are_rejected_without_v11_metadata(name):
    result = adapt_outbox_event(event(name), None)
    assert isinstance(result, Incompatible)
    if name in REPRESENTABLE:
        assert {f"boundary.{f}" for f in EVENT_BOUNDARY_FIELDS} <= set(result.missing_fields) or (
            result.error_code is not ErrorCode.NOT_IMPLEMENTED and result.missing_fields
        )


@pytest.mark.parametrize("name", NOT_REPRESENTABLE)
def test_d03_non_representable_transitions_stay_rejected_with_full_metadata(name):
    result = adapt(name)
    assert isinstance(result, Incompatible) and result.error_code is ErrorCode.NOT_IMPLEMENTED
    assert result.reasons


@pytest.mark.parametrize("name", REPRESENTABLE)
def test_d04_complete_events_map_with_identity_and_version_preserved(name):
    source = event(name)
    mapped = ok(adapt(name))
    assert mapped.event_id == source["event_id"]
    assert mapped.transaction_id == source["transaction_id"]
    assert mapped.route_id == source["route_id"]
    assert mapped.remitter_bank_id == source["bank_id"]
    assert mapped.state_version == source["transaction_version"]
    assert mapped.amount_minor == source["amount_paise"]
    assert mapped.counted_check_attempts == source["payload"]["checks_used"]
    assert mapped.source_sequence == BOUNDARY["events"]["per_event"][name]["source_sequence"]
    assert mapped.event_timestamp == datetime.fromisoformat(source["timestamp"])
    if "deadline_at" in source["payload"]:
        assert mapped.deadline_at == datetime.fromisoformat(source["payload"]["deadline_at"])
        assert mapped.deadline_policy_id == "research-60min"


def test_d05_debit_becomes_payment_initiated():
    mapped = ok(adapt("debit"))
    assert mapped.event_type is TransactionEventType.PAYMENT_INITIATED
    assert (mapped.ledger_state, mapped.financial_outcome) == (
        LedgerState.DEBITED,
        FinancialOutcome.UNKNOWN,
    )
    assert mapped.beneficiary_bank_id == "B"  # from Track B's own debit payload
    conflict = adapt("debit", beneficiary_bank_id="C")
    assert isinstance(conflict, Incompatible)
    assert conflict.error_code is ErrorCode.CONFLICTING_EVENT


def test_d06_timeout_keeps_the_outcome_unknown():
    mapped = ok(adapt("timeout"))
    assert mapped.event_type is TransactionEventType.PAYMENT_TIMED_OUT
    assert mapped.financial_outcome is FinancialOutcome.UNKNOWN
    unlabelled = adapt("timeout", ambiguity_cause=None)
    assert isinstance(unlabelled, Incompatible)
    assert unlabelled.missing_fields == ("boundary.ambiguity_cause",)


def test_d07_a_timeout_claiming_failure_is_refused_not_mapped():
    claimed = event("timeout")
    claimed["payload"]["financial_outcome"] = "FAILURE"
    result = adapt_outbox_event(claimed, event_boundary("timeout"))
    assert isinstance(result, Incompatible)
    assert result.error_code is ErrorCode.TIMEOUT_CANNOT_IMPLY_OUTCOME


@pytest.mark.parametrize("name", ["timeout", "check_started", "check_inconclusive", "escalated"])
def test_d08_unknown_is_never_mapped_to_failure(name):
    mapped = ok(adapt(name))
    assert mapped.financial_outcome is FinancialOutcome.UNKNOWN
    assert TRACKB_OUTCOME["UNKNOWN"] is FinancialOutcome.UNKNOWN


def test_d09_pending_original_response_requires_evidence():
    missing = adapt("timeout", ambiguity_cause="PENDING_RESPONSE")
    assert isinstance(missing, Incompatible)
    assert missing.error_code is ErrorCode.EVIDENCE_REQUIRED
    evidence = {
        "evidence_id": "EV-ORIG-1",
        "evidence_kind": "ORIGINAL_PAYMENT_RESPONSE",
        "issuer_id": "SWITCH",
        "response_version": 1,
    }
    mapped = ok(adapt("timeout", ambiguity_cause="PENDING_RESPONSE", evidence=evidence))
    assert mapped.event_type is TransactionEventType.PAYMENT_RESPONSE_RECEIVED
    assert mapped.financial_outcome is FinancialOutcome.PENDING


def test_d10_status_check_dispatch_needs_a_check_identifier():
    assert ok(adapt("check_started")).check_id == "CK-TXN-A1-0001-1"
    result = adapt("check_started", check_id=None)
    assert isinstance(result, Incompatible) and "boundary.check_id" in result.missing_fields


@pytest.mark.parametrize(
    ("fact", "kind", "extra"),
    [
        ({"kind": "ANSWERED", "check_result": "UNKNOWN"}, "STATUS_CHECK_ANSWERED", "UNKNOWN"),
        ({"kind": "TIMED_OUT", "result_code": "CHECK_TIMEOUT"}, "STATUS_CHECK_TIMED_OUT", None),
        ({"kind": "REJECTED", "result_code": "RATE_LIMITED"}, "STATUS_CHECK_REJECTED", None),
    ],
)
def test_d11_overloaded_check_result_needs_an_explicit_fact(fact, kind, extra):
    mapped = ok(adapt("check_inconclusive", check_fact=fact))
    assert mapped.event_type == kind
    assert mapped.financial_outcome is FinancialOutcome.UNKNOWN
    assert (mapped.check_result.value if mapped.check_result else None) == extra


@pytest.mark.parametrize(
    "fact",
    [
        None,
        {"kind": "ANSWERED", "check_result": "AUTHORITATIVE_FAILURE"},
        {"kind": "ANSWERED"},
        {"kind": "TIMED_OUT"},
        {"kind": "GUESS", "result_code": "CHECK_TIMEOUT"},
    ],
)
def test_d12_missing_or_unsafe_check_facts_are_rejected(fact):
    result = adapt("check_inconclusive", check_fact=fact)
    assert isinstance(result, Incompatible)
    assert result.missing_fields == ("boundary.check_fact",)


def test_d13_check_fact_must_be_consistent_with_v11_rules():
    timed_out_rate_limit = adapt(
        "check_inconclusive", check_fact={"kind": "TIMED_OUT", "result_code": "RATE_LIMITED"}
    )
    assert isinstance(timed_out_rate_limit, Incompatible)
    pending_without_evidence = adapt(
        "check_inconclusive", check_fact={"kind": "ANSWERED", "check_result": "PENDING"}
    )
    assert isinstance(pending_without_evidence, Incompatible)
    assert pending_without_evidence.error_code is ErrorCode.EVIDENCE_REQUIRED


@pytest.mark.parametrize(
    ("name", "result", "outcome", "ledger"),
    [
        ("succeeded", "AUTHORITATIVE_SUCCESS", "SUCCESS", "CONFIRMED_SUCCESS"),
        ("failed", "AUTHORITATIVE_FAILURE", "FAILURE", "CONFIRMED_FAILED"),
    ],
)
def test_d14_confirmed_outcomes_require_evidence(name, result, outcome, ledger):
    mapped = ok(adapt(name))
    assert mapped.event_type is TransactionEventType.STATUS_CHECK_ANSWERED
    assert (mapped.check_result, mapped.financial_outcome, mapped.ledger_state) == (
        result,
        outcome,
        ledger,
    )
    assert mapped.evidence_reference.evidence_id.startswith("EV-SW-")
    missing = adapt(name, evidence=None)
    assert isinstance(missing, Incompatible)
    assert missing.error_code is ErrorCode.EVIDENCE_REQUIRED
    assert any(f.startswith("boundary.evidence") for f in missing.missing_fields)


def test_d15_original_response_evidence_yields_payment_response_received():
    evidence = {
        "evidence_id": "EV-ORIG-9",
        "evidence_kind": "ORIGINAL_PAYMENT_RESPONSE",
        "issuer_id": "SWITCH",
        "response_version": 1,
    }
    mapped = ok(adapt("succeeded", evidence=evidence, check_id=None))
    assert mapped.event_type is TransactionEventType.PAYMENT_RESPONSE_RECEIVED
    assert mapped.check_id is None


def test_d16_evidence_issuer_must_match_track_b_evidence_source():
    forged = {**event_boundary("succeeded")["evidence"], "issuer_id": "BANK-B"}
    result = adapt("succeeded", evidence=forged)
    assert isinstance(result, Incompatible) and result.error_code is ErrorCode.EVIDENCE_CONFLICT


def test_d17_reversal_completion_requires_the_committed_operation():
    mapped = ok(adapt("reversal_completed"))
    assert mapped.event_type is TransactionEventType.REVERSAL_COMPLETED
    assert (mapped.ledger_state, mapped.financial_outcome) == (
        LedgerState.REVERSED,
        FinancialOutcome.FAILURE,
    )
    assert mapped.state_version == 6  # REVERSAL_INITIATED (v5) is skipped, not renumbered
    uncommitted = event("reversal_completed")
    del uncommitted["payload"]["operation_id"]
    result = adapt_outbox_event(uncommitted, event_boundary("reversal_completed"))
    assert isinstance(result, Incompatible)
    assert "event.payload.operation_id" in result.missing_fields


def test_d18_escalation_maps_to_reconciliation_handoff():
    mapped = ok(adapt("escalated"))
    assert mapped.event_type is TransactionEventType.RECONCILIATION_HANDOFF
    assert mapped.ledger_state is LedgerState.RECONCILIATION_PENDING


def test_d19_duplicate_deliveries_keep_identity_for_downstream_idempotency():
    first, second = ok(adapt("succeeded")), ok(adapt("succeeded"))
    assert first == second and encode(first) == encode(second)


def test_d20_missing_identity_and_correlation_are_reported():
    bare = event("closed")
    bare["correlation_id"] = None
    result = adapt_outbox_event(bare, event_boundary("closed"))
    assert isinstance(result, Incompatible) and "event.correlation_id" in result.missing_fields
    no_policy = adapt("closed", deadline_policy_id=None)
    assert "boundary.deadline_policy_id" in no_policy.missing_fields
    unknown = event("closed")
    unknown["payload"]["financial_outcome"] = "MAYBE"
    assert isinstance(adapt_outbox_event(unknown, event_boundary("closed")), Incompatible)


def test_d21_unmapped_track_b_transition_is_rejected():
    odd = event("closed")
    odd["payload"]["previous_state"] = "DEBITED"
    result = adapt_outbox_event(odd, event_boundary("closed"))
    assert isinstance(result, Incompatible) and result.error_code is ErrorCode.NOT_IMPLEMENTED


def test_d22_adapters_do_not_mutate_their_inputs():
    source, boundary = event("succeeded"), event_boundary("succeeded")
    snapshot = copy.deepcopy((source, boundary))
    adapt_outbox_event(source, boundary)
    assert (source, boundary) == snapshot


# ===================================================================== E — telemetry


def tel_boundary(kind: str = "trusted", **overrides) -> dict:
    """ADDITIONAL metadata (boundary.json); Track B does not emit it at 8b863da."""

    return {**copy.deepcopy(BOUNDARY["telemetry"][kind]), **overrides}


def adapt_tel(assessment="assessment_trusted", boundary="trusted", **overrides):
    return adapt_validated_telemetry(
        copy.deepcopy(TELEMETRY["observation"]),
        copy.deepcopy(TELEMETRY[assessment]),
        tel_boundary(boundary, **overrides) if boundary else None,
    )


def test_e01_complete_trusted_telemetry_maps_with_values_preserved():
    result = adapt_tel()
    envelope = ok(result)
    obs, t = TELEMETRY["observation"], envelope.telemetry
    assert (t.source_id, t.sequence, t.route_id) == (obs["source_id"], obs["sequence"], "A1-B")
    assert t.observation_timestamp == datetime(2026, 10, 18, 10, 0, 15, tzinfo=UTC)
    assert (t.p50_latency_ms, t.p95_latency_ms) == (180.0, 420.0)
    assert (t.observed_throughput_rps, t.timeout_rate, t.queue_depth) == (38.5, 0.02, 7)
    assert envelope.trust_status == "AUTHENTICATED" and not envelope.is_flagged
    assert envelope.validator_id == "trackb-telemetry-validator"
    assert set(result.dropped_fields) == {"health_score", "event_id"}


def test_e02_real_track_b_telemetry_lacks_v11_metadata():
    result = adapt_tel(boundary=None)
    assert isinstance(result, Incompatible)
    assert set(result.missing_fields) == {f"boundary.{f}" for f in TELEMETRY_BOUNDARY_FIELDS}


@pytest.mark.parametrize(
    "field", ["observation_window_start", "observation_window_end", "latency_sample_count"]
)
def test_e03_missing_window_or_sample_count_is_rejected(field):
    result = adapt_tel(**{field: None})
    assert isinstance(result, Incompatible) and result.missing_fields == (f"boundary.{field}",)


def test_e04_missing_source_sequence_is_rejected_not_derived_from_time():
    obs = copy.deepcopy(TELEMETRY["observation"])
    del obs["sequence"]
    result = adapt_validated_telemetry(obs, TELEMETRY["assessment_trusted"], tel_boundary())
    assert isinstance(result, Incompatible) and "observation.sequence" in result.missing_fields


def test_e05_flags_are_preserved_and_flagged_data_stays_flagged():
    envelope = ok(adapt_tel("assessment_suspicious", "suspicious"))
    assert envelope.validation_flags == ("CROSS_CHECK_DEVIATION",)
    assert envelope.is_flagged and envelope.cross_check_status == "INCONSISTENT"


def test_e06_suspicious_data_cannot_be_laundered_as_clean():
    result = adapt_tel("assessment_suspicious", "trusted")
    assert isinstance(result, Incompatible)
    assert result.missing_fields == ("boundary.validation_flags",)
    conflicting = adapt_tel("assessment_trusted", "suspicious")
    assert isinstance(conflicting, Incompatible)


@pytest.mark.parametrize("trust", ["UNAUTHENTICATED", "UNKNOWN_SOURCE"])
def test_e07_unauthenticated_data_can_never_be_trusted(trust):
    result = adapt_tel(trust_status=trust)
    assert isinstance(result, Incompatible) and result.error_code is ErrorCode.UNTRUSTED_SOURCE
    flagged = ok(adapt_tel("assessment_suspicious", "suspicious", trust_status=trust))
    assert flagged.trust_status == trust  # forwarded only as untrusted


def test_e08_rejected_samples_are_not_forwarded():
    result = adapt_tel("assessment_rejected")
    assert isinstance(result, Incompatible) and result.error_code is ErrorCode.UNTRUSTED_SOURCE
    assert "unauthenticated source" in result.reasons


def test_e09_identity_mismatches_are_rejected():
    other_route = {**TELEMETRY["assessment_trusted"], "route_id": "A1-C"}
    result = adapt_validated_telemetry(TELEMETRY["observation"], other_route, tel_boundary())
    assert isinstance(result, Incompatible)
    bad_experiment = adapt_tel(experiment_id="bad experiment id")
    assert isinstance(bad_experiment, Incompatible)
    inconsistent = adapt_tel(cross_check_status="INCONSISTENT")
    assert isinstance(inconsistent, Incompatible)  # INCONSISTENT needs the deviation flag


# ================================================================== F — status results


def status_boundary(**overrides) -> dict:
    """ADDITIONAL metadata (boundary.json); Track B does not emit it at 8b863da."""

    return {**copy.deepcopy(BOUNDARY["status_checks"]["success"]), **overrides}


def check(name, boundary=None, **overrides):
    case = copy.deepcopy(CHECKS[name])
    return adapt_status_check(case["outcome"], case["evidence"], boundary)


def test_f01_verified_success_is_authoritative():
    result = ok(check("success", status_boundary()))
    assert isinstance(result, StatusCheckResult)
    assert result.check_result is CheckResultKind.AUTHORITATIVE_SUCCESS and result.is_authoritative
    assert result.counted_check_attempts == 2 and result.counts_against_limit
    assert result.responded_at == datetime(2026, 10, 18, 10, 0, 6, 39000, tzinfo=UTC)


@pytest.mark.parametrize(
    "boundary",
    [
        None,
        {"verification": None},
        {
            "verification": {
                "status": "VERIFIED",
                "verifier_id": "v",
                "key_id": "other-key",
                "verified_at": "2026-10-18T10:00:06Z",
            }
        },  # fmt: skip
        {
            "verification": {
                "status": "CLAIMED",
                "verifier_id": "v",
                "key_id": "switch-key-1",
                "verified_at": "2026-10-18T10:00:06Z",
            }
        },  # fmt: skip
    ],
    ids=["no-metadata", "bare-authenticated-flag", "key-mismatch", "not-verified"],
)
def test_f02_confirmed_results_need_a_complete_verification_record(boundary):
    full = None if boundary is None else status_boundary(**boundary)
    for name in ("success", "failed"):
        result = check(name, full)
        assert isinstance(result, Incompatible)
        assert result.error_code is ErrorCode.EVIDENCE_REQUIRED


def test_f03_pending_is_preserved_and_not_authoritative():
    unverified = status_boundary(verification=None)
    result = ok(check("pending", unverified))
    assert result.check_result is CheckResultKind.PENDING
    assert result.evidence_verified is False and not result.is_authoritative
    no_evidence = check("pending", status_boundary(evidence_reference=None))
    assert isinstance(no_evidence, Incompatible)


def test_f04_not_found_is_unknown_and_needs_no_evidence():
    result = ok(
        check("not_found", status_boundary(evidence_reference=None, evidence_signature=None))
    )
    assert result.check_result is CheckResultKind.UNKNOWN
    assert TRACKB_CHECK_RESULT["NOT_FOUND"] is CheckResultKind.UNKNOWN
    assert CheckResultKind.AUTHORITATIVE_FAILURE not in (
        TRACKB_CHECK_RESULT[s] for s in ("PENDING", "NOT_FOUND")
    )


def test_f05_pre_dispatch_rejection_is_not_a_completed_check():
    result = ok(check("not_admitted", status_boundary()))
    assert isinstance(result, ErrorResponse)
    assert (result.error_code, result.counts_against_limit) == (ErrorCode.RATE_LIMITED, False)
    unlabelled = check("not_admitted_unlabelled", status_boundary())
    assert isinstance(unlabelled, Incompatible)


@pytest.mark.parametrize(
    ("name", "code"),
    [("read_timeout", ErrorCode.CHECK_TIMEOUT), ("connect_error", ErrorCode.UPSTREAM_UNAVAILABLE)],
)
def test_f06_transport_failures_count_but_never_decide_the_outcome(name, code):
    result = ok(check(name, status_boundary()))
    assert isinstance(result, ErrorResponse)
    assert (result.error_code, result.counts_against_limit, result.retryable) == (code, True, True)


def test_f07_failed_signature_is_not_an_answer():
    result = ok(check("unauthenticated", status_boundary()))
    assert isinstance(result, ErrorResponse)
    assert result.error_code is ErrorCode.EVIDENCE_SIGNATURE_INVALID
    assert result.counts_against_limit is True


def test_f08_missing_check_identity_or_counts_are_rejected():
    for field in ("check_id", "counted_check_attempts", "experiment_id"):
        result = check("success", status_boundary(**{field: None}))
        assert isinstance(result, Incompatible) and f"boundary.{field}" in result.missing_fields


def test_f09_conflicting_status_is_rejected():
    case = copy.deepcopy(CHECKS["success"])
    case["outcome"]["status"] = "FAILED"
    result = adapt_status_check(case["outcome"], case["evidence"], status_boundary())
    assert isinstance(result, Incompatible) and result.error_code is ErrorCode.EVIDENCE_CONFLICT


# ========================================================== G — integration fixtures


def test_g01_adapted_lifecycle_applies_to_the_transaction_twin():
    clock = ManualClock(datetime(2026, 10, 18, 10, 0, 30, tzinfo=UTC))
    twin = TransactionTwinRegistry(POLICY, clock)
    twin.register_route(
        RouteDescriptor(
            schema_version="1.1",
            experiment_id="EXP-INT-1",
            route_id="A1-B",
            remitter_bank_id="A1",
            switch_id="SWITCH",
            beneficiary_bank_id="B",
        )
    )
    lifecycle = ("debit", "timeout", "check_started", "check_inconclusive", "check_started_2",
                 "succeeded", "closed")  # fmt: skip
    statuses = [twin.apply_event(ok(adapt(name))).status for name in lifecycle]
    assert statuses == [ApplyStatus.APPLIED] * len(lifecycle)
    assert twin.apply_event(ok(adapt("succeeded"))).status is ApplyStatus.DUPLICATE
    projection = twin.snapshot("EXP-INT-1", "TXN-A1-0001")
    assert projection.terminal and projection.financial_outcome is FinancialOutcome.SUCCESS
    assert projection.checks_used == 2 and projection.version == 7


def test_g02_reversal_lifecycle_reports_the_skipped_intermediate_version():
    clock = ManualClock(datetime(2026, 10, 18, 10, 2, tzinfo=UTC))
    twin = TransactionTwinRegistry(POLICY, clock)
    first = ok(adapt("failed"))
    # The twin learns this transaction from its first event; v1.1 needs PAYMENT_INITIATED first.
    assert twin.apply_event(first).status is ApplyStatus.UNKNOWN_TRANSACTION
    assert isinstance(adapt("reversal_initiated"), Incompatible)


def test_g03_adapted_telemetry_feeds_the_route_twin_and_flags_stay_separate():
    clock = ManualClock(datetime(2026, 10, 18, 10, 0, 17, tzinfo=UTC))
    twin = RouteTwinRegistry(
        RouteTwinConfig(
            model_version="stage03",
            max_observation_age_s=30.0,
            max_future_skew_s=2.0,
            freshness_threshold_s=30.0,
        ),
        clock,
    )
    twin.register_route(
        RouteDescriptor(
            schema_version="1.1",
            experiment_id="EXP-INT-1",
            route_id="A1-B",
            remitter_bank_id="A1",
            switch_id="SWITCH",
            beneficiary_bank_id="B",
        ),
        ["twin-probe-1"],
    )
    clean = ok(adapt_tel())
    assert isinstance(clean, ValidatedRouteTelemetry)
    assert twin.ingest(clean).status is IngestStatus.APPLIED
    snapshot = twin.snapshot("EXP-INT-1", "A1-B")
    assert snapshot.latest_trusted_observation == clean.telemetry
    next_window = tel_boundary(
        "suspicious",
        observation_window_start="2026-10-18T10:00:15Z",
        observation_window_end="2026-10-18T10:00:16Z",
        validated_at="2026-10-18T10:00:16.200000Z",
    )
    inflated = {
        **TELEMETRY["observation"],
        "event_id": "twin-probe-1-A1-B-42",
        "sequence": 42,
        "timestamp": "2026-10-18T10:00:16Z",
        "throughput_tps": 50.0,
    }
    assessment = {**TELEMETRY["assessment_suspicious"], "event_id": "twin-probe-1-A1-B-42"}
    flagged = ok(adapt_validated_telemetry(inflated, assessment, next_window))
    assert twin.ingest(flagged).status is IngestStatus.APPLIED
    snapshot = twin.snapshot("EXP-INT-1", "A1-B")
    assert snapshot.latest_trusted_observation == clean.telemetry  # inflation not trusted
    assert snapshot.latest_flagged_observation == flagged


def test_g04_rule_table_documents_every_status():
    statuses = {rule.status for rule in OUTBOX_RULES}
    assert statuses == set(MappingStatus)
    for rule in OUTBOX_RULES:
        assert rule.note
        assert (rule.v11_event_type is None) == (rule.status is not MappingStatus.DIRECT)


# ============================================================ M — timing-policy alignment


def test_m02_track_b_default_backoff_is_not_a_v11_timing_policy():
    settings = OTHER["trackb_settings"]
    result = express_trackb_timing_policy(settings, policy_id="trackb-default")
    assert isinstance(result, Incompatible) and result.error_code is ErrorCode.NOT_IMPLEMENTED
    assert "exponential backoff" in result.reasons[0]


def test_m03_constant_spacing_is_expressible_but_differs_from_the_research_policy():
    settings = {**OTHER["trackb_settings"], "check_backoff_max_seconds": 1.0}
    policy = ok(express_trackb_timing_policy(settings, policy_id="trackb-constant"))
    assert policy.regime == "RESEARCH_SCALED" and policy.verification_status != "VERIFIED"
    assert (policy.max_counted_checks, policy.min_check_spacing_s) == (6, 1.0)
    research = research_timing_policy()
    assert timing_policy_mismatches(timing_policy_reference(research), policy)
    same_id = policy.model_copy(update={"policy_id": research.policy_id})
    (problem,) = timing_policy_mismatches(timing_policy_reference(research), same_id)
    assert "different content" in problem
