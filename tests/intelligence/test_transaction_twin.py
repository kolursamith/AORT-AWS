"""Stage 2 Transaction Digital Twin tests. Groups A–I follow the Stage 2 acceptance list.

Deterministic: time comes from ManualClock; there are no sleeps, network, database or cloud
resources. Every event is a contract-valid synthetic TransactionEvent unless a test forges
one with ``model_construct`` to prove the twin re-validates its input.
"""

import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from contracts.v1_1 import (
    ErrorCode,
    RouteDescriptor,
    TimingPolicy,
    TransactionEvent,
    TransactionProjection,
)
from intelligence.common import ManualClock
from intelligence.twin import (
    ApplyStatus,
    EligibilityStatus,
    TransactionTwinConfig,
    TransactionTwinInvariantError,
    TransactionTwinRegistry,
    UnknownTransactionError,
    evaluate_eligibility,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
EXP1, EXP2 = "EXP-1", "EXP-2"
TXN = "TXN1001"
ROUTE = "A1-B"
POLICY_ID = "research-60min"
DEADLINE = 3615.0  # timeout at t=15 plus a 3600 s research deadline


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def make_policy(**overrides) -> TimingPolicy:
    base = {
        "schema_version": "1.1",
        "policy_id": POLICY_ID,
        "regime": "RESEARCH_SCALED",
        "verification_status": "RESEARCH_PARAMETER",
        "first_check_delay_s": 90.0,
        "first_check_reference": "INITIATED",
        "min_check_spacing_s": 90.0,
        "max_counted_checks": 3,
        "check_window_s": 7200.0,
        "stop_error_codes": [],
        "client_timeout_s": 15.0,
        "check_timeout_s": 10.0,
        "deadline_rule": {"kind": "FIXED_DURATION", "duration_s": 3600.0},
        "escalation_window_s": 600.0,
        "escalation_extra_checks": 0,
    }
    return TimingPolicy(**{**base, **overrides})


def evidence(n: int = 1, kind: str = "STATUS_CHECK_RESPONSE") -> dict:
    return {
        "evidence_id": f"EV-{n}",
        "evidence_kind": kind,
        "issuer_id": "SW",
        "response_version": n,
    }


def event(kind: str, version: int, *, txn=TXN, exp=EXP1, t=0.0, ledger="DEBITED",
          outcome="UNKNOWN", checks=0, event_id=None, route=ROUTE, remitter="A1",
          beneficiary="B", amount=50000, **extra) -> TransactionEvent:  # fmt: skip
    return TransactionEvent(
        schema_version="1.1",
        experiment_id=exp,
        event_id=event_id or f"{exp}:{txn}:v{version}",
        transaction_id=txn,
        route_id=route,
        remitter_bank_id=remitter,
        beneficiary_bank_id=beneficiary,
        event_type=kind,
        ledger_state=ledger,
        financial_outcome=outcome,
        state_version=version,
        amount_minor=amount,
        event_timestamp=at(t),
        source_id="remitter-A1",
        source_sequence=version,
        correlation_id=f"PAY-{txn}",
        counted_check_attempts=checks,
        **extra,
    )


def initiated(**kw):
    return event("PAYMENT_INITIATED", 1, **kw)


def timed_out(version=2, t=15.0, deadline: float | None = DEADLINE, **kw):
    extra = (
        {} if deadline is None else {"deadline_at": at(deadline), "deadline_policy_id": POLICY_ID}
    )
    return event("PAYMENT_TIMED_OUT", version, t=t, **extra, **kw)


def dispatched(version, check="CHK-1", t=100.0, checks=1, **kw):
    return event("STATUS_CHECK_DISPATCHED", version, check_id=check, t=t, checks=checks, **kw)


ANSWERS = {
    "AUTHORITATIVE_SUCCESS": {"ledger": "CONFIRMED_SUCCESS", "outcome": "SUCCESS"},
    "AUTHORITATIVE_FAILURE": {"ledger": "CONFIRMED_FAILED", "outcome": "FAILURE"},
    "PENDING": {"ledger": "DEBITED", "outcome": "PENDING"},
    "UNKNOWN": {"ledger": "DEBITED", "outcome": "UNKNOWN"},
}


def answered(version, result, check="CHK-1", t=101.0, checks=1, **kw):
    fields = dict(ANSWERS[result])
    if result != "UNKNOWN":
        fields["evidence_reference"] = evidence(version)
    fields.update(kw)
    return event(
        "STATUS_CHECK_ANSWERED", version, check_id=check, check_result=result, t=t, checks=checks,
        **fields,
    )  # fmt: skip


def rejected(version, code, check="CHK-1", t=100.0, checks=0, **kw):
    return event("STATUS_CHECK_REJECTED", version, check_id=check, result_code=code, t=t,
                 checks=checks, **kw)  # fmt: skip


def check_timed_out(version, check="CHK-1", t=111.0, checks=1, code="CHECK_TIMEOUT", **kw):
    return event("STATUS_CHECK_TIMED_OUT", version, check_id=check, result_code=code, t=t,
                 checks=checks, **kw)  # fmt: skip


def forge(valid: TransactionEvent, **changes) -> TransactionEvent:
    """An event built without validation, as a buggy or hostile producer could."""

    return TransactionEvent.model_construct(**{**valid.__dict__, **changes})


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(at(0))


@pytest.fixture
def twin(clock) -> TransactionTwinRegistry:
    return TransactionTwinRegistry(make_policy(), clock)


def apply_all(registry, *events):
    return [registry.apply_event(e) for e in events]


def snap(registry, txn=TXN, exp=EXP1) -> TransactionProjection:
    return registry.snapshot(exp, txn)


def ambiguous(registry):
    apply_all(registry, initiated(), timed_out())


# ============================================================ Group A — creation, identity


def test_a01_payment_initiated_creates_an_active_projection(twin):
    result = twin.apply_event(initiated())
    assert result.status is ApplyStatus.APPLIED and result.projection_version == 1
    p = snap(twin)
    assert p.workflow_state == "ACTIVE"
    assert p.financial_outcome == "UNKNOWN"
    assert p.ledger_state_observed == "DEBITED"
    assert p.version == 1 and p.checks_used == 0
    assert p.ambiguity_created_at is None and p.check_in_flight_id is None
    assert p.last_evidence_reference is None and p.initiated_at == at(0)


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("experiment_id", EXP1),
        ("transaction_id", TXN),
        ("route_id", ROUTE),
        ("remitter_bank_id", "A1"),
        ("beneficiary_bank_id", "B"),
        ("amount_minor", 50000),
        ("currency", "INR"),
    ],
)
def test_a02_a05_identity_fields_are_copied_from_the_event(twin, field, expected):
    twin.apply_event(initiated())
    assert getattr(snap(twin), field) == expected


def test_a06_duplicate_initiation_is_idempotent(twin):
    twin.apply_event(initiated())
    redelivered = twin.apply_event(initiated())
    republished = twin.apply_event(initiated(event_id="new-delivery-id"))
    assert redelivered.status is ApplyStatus.DUPLICATE
    assert republished.status is ApplyStatus.DUPLICATE
    assert twin.transaction_ids(EXP1) == (TXN,)
    assert snap(twin).version == 1


def test_a06b_incompatible_initiation_is_a_conflict(twin):
    twin.apply_event(initiated())
    result = twin.apply_event(
        initiated(event_id="other", deadline_at=at(99), deadline_policy_id=POLICY_ID)
    )
    assert result.status is ApplyStatus.CONFLICT
    assert result.error_code is ErrorCode.CONFLICTING_EVENT
    assert snap(twin).workflow_state == "QUARANTINED"


def test_a07_same_transaction_id_in_another_experiment_is_isolated(twin):
    twin.apply_event(initiated())
    twin.apply_event(initiated(exp=EXP2, amount=1))
    twin.apply_event(timed_out(exp=EXP2, amount=1))
    assert snap(twin, exp=EXP1).workflow_state == "ACTIVE"
    assert snap(twin, exp=EXP2).workflow_state == "AMBIGUOUS"
    assert snap(twin, exp=EXP2).amount_minor == 1


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"route": "A1-C"}, ErrorCode.ROUTE_IDENTITY_MISMATCH),
        ({"remitter": "A2"}, ErrorCode.ROUTE_IDENTITY_MISMATCH),
        ({"beneficiary": "C"}, ErrorCode.ROUTE_IDENTITY_MISMATCH),
        ({"amount": 1}, ErrorCode.CONFLICTING_EVENT),
        ({"currency": "USD"}, ErrorCode.CONFLICTING_EVENT),
    ],
    ids=["route", "remitter", "beneficiary", "amount", "currency"],
)
def test_a08_a10_identity_mismatch_is_rejected_and_quarantined(twin, change, code):
    twin.apply_event(initiated())
    result = twin.apply_event(timed_out(**change))
    assert result.status is ApplyStatus.IDENTITY_MISMATCH
    assert result.error_code is code
    p = snap(twin)
    assert p.version == 1 and p.ambiguity_created_at is None
    assert p.workflow_state == "QUARANTINED" and p.quarantine_reason == "IDENTITY_MISMATCH"


def test_a11_initiation_is_checked_against_registered_routes(clock):
    route = RouteDescriptor(
        schema_version="1.1",
        experiment_id=EXP1,
        route_id=ROUTE,
        remitter_bank_id="A1",
        switch_id="SW",
        beneficiary_bank_id="B",
    )
    strict = TransactionTwinRegistry(
        make_policy(), clock, TransactionTwinConfig(require_registered_routes=True), [route]
    )
    assert strict.apply_event(initiated(beneficiary="C")).status is ApplyStatus.IDENTITY_MISMATCH
    assert strict.apply_event(initiated(route="A1-Z")).status is ApplyStatus.IDENTITY_MISMATCH
    assert strict.apply_event(initiated()).applied


# ============================================================ Group B — ambiguity


def test_b01_b03_payment_timeout_creates_ambiguity(twin):
    twin.apply_event(initiated())
    result = twin.apply_event(timed_out())
    assert result.applied
    p = snap(twin)
    assert p.workflow_state == "AMBIGUOUS"
    assert p.financial_outcome == "UNKNOWN"
    assert p.ambiguity_created_at == at(15)


def test_b04_initiation_without_timeout_stays_active(twin):
    twin.apply_event(initiated())
    assert snap(twin).workflow_state == "ACTIVE"
    assert twin.eligibility(EXP1, TXN).status is EligibilityStatus.NOT_AMBIGUOUS


def test_b05_timeout_never_moves_the_ledger(twin, clock):
    ambiguous(twin)
    clock.advance(10_000)
    p = snap(twin)
    assert p.ledger_state_observed == "DEBITED"
    assert p.financial_outcome == "UNKNOWN"
    assert p.version == 2


def test_b06_duplicate_timeout_has_no_effect(twin):
    ambiguous(twin)
    before = snap(twin)
    assert twin.apply_event(timed_out()).status is ApplyStatus.DUPLICATE
    assert snap(twin) == before


def test_b07_late_original_response_resolves_ambiguity(twin):
    ambiguous(twin)
    late = event(
        "PAYMENT_RESPONSE_RECEIVED",
        3,
        t=40,
        ledger="CONFIRMED_SUCCESS",
        outcome="SUCCESS",
        evidence_reference=evidence(3, "ORIGINAL_PAYMENT_RESPONSE"),
    )
    assert twin.apply_event(late).applied
    p = snap(twin)
    assert (p.workflow_state, p.financial_outcome, p.terminal) == ("CLOSED", "SUCCESS", True)


def test_b08_pending_original_response_remains_unresolved(twin):
    ambiguous(twin)
    pending = event(
        "PAYMENT_RESPONSE_RECEIVED",
        3,
        t=40,
        outcome="PENDING",
        evidence_reference=evidence(3, "ORIGINAL_PAYMENT_RESPONSE"),
    )
    assert twin.apply_event(pending).applied
    p = snap(twin)
    assert p.workflow_state == "AMBIGUOUS" and p.financial_outcome == "PENDING"
    assert not p.terminal


# ============================================================ Group C — status checks


def test_c01_c04_dispatch_moves_to_check_in_flight(twin):
    ambiguous(twin)
    assert twin.apply_event(dispatched(3, check="CHK-7", t=100, checks=1)).applied
    p = snap(twin)
    assert p.workflow_state == "CHECK_IN_FLIGHT"
    assert p.check_in_flight_id == "CHK-7" and p.last_check_id == "CHK-7"
    assert p.last_check_at == at(100)
    assert p.checks_used == 1


def test_c05_pending_answer_returns_to_unresolved(twin):
    ambiguous(twin)
    apply_all(twin, dispatched(3), answered(4, "PENDING"))
    p = snap(twin)
    assert p.workflow_state == "AMBIGUOUS" and p.financial_outcome == "PENDING"
    assert p.check_in_flight_id is None and p.last_evidence_reference is not None


def test_c06_unknown_answer_confirms_nothing(twin):
    ambiguous(twin)
    apply_all(twin, dispatched(3), answered(4, "UNKNOWN"))
    p = snap(twin)
    assert p.workflow_state == "AMBIGUOUS" and p.financial_outcome == "UNKNOWN"
    assert p.last_evidence_reference is None and not p.terminal


def test_c07_check_timeout_does_not_imply_failure(twin):
    ambiguous(twin)
    apply_all(twin, dispatched(3), check_timed_out(4, checks=1))
    p = snap(twin)
    assert p.workflow_state == "AMBIGUOUS" and p.financial_outcome == "UNKNOWN"
    assert p.ledger_state_observed == "DEBITED"
    assert p.last_rejection_code == "CHECK_TIMEOUT" and p.checks_used == 1


@pytest.mark.parametrize("code", ["CREDIT_EXHAUSTED", "RATE_LIMITED", "NOT_YET_ELIGIBLE"])
def test_c08_c09_pre_dispatch_rejection_adds_no_attempt(twin, clock, code):
    ambiguous(twin)
    assert twin.apply_event(rejected(3, code, checks=0)).applied
    p = snap(twin)
    assert p.checks_used == 0 and p.last_rejection_code == code
    assert p.workflow_state == "AMBIGUOUS"
    clock.set_time(at(95))
    assert twin.eligibility(EXP1, TXN).status is EligibilityStatus.ELIGIBLE


def test_c10_in_flight_transaction_is_not_listed(twin, clock):
    ambiguous(twin)
    twin.apply_event(dispatched(3))
    clock.set_time(at(500))
    assert twin.eligible_transactions(EXP1, ROUTE) == ()
    assert twin.eligibility(EXP1, TXN).status is EligibilityStatus.CHECK_IN_FLIGHT


def test_c11_check_limit_is_reached_from_backend_counts(twin, clock):
    ambiguous(twin)
    apply_all(
        twin,
        dispatched(3, "C1", t=100, checks=1),
        answered(4, "UNKNOWN", "C1", t=101, checks=1),
        dispatched(5, "C2", t=200, checks=2),
        answered(6, "UNKNOWN", "C2", t=201, checks=2),
        dispatched(7, "C3", t=300, checks=3),
        answered(8, "UNKNOWN", "C3", t=301, checks=3),
    )
    clock.set_time(at(1000))
    assert twin.eligibility(EXP1, TXN).status is EligibilityStatus.CHECK_LIMIT_REACHED
    assert snap(twin).next_eligible_at is None


def test_c11b_backend_limit_rejection_is_honoured(twin, clock):
    ambiguous(twin)
    twin.apply_event(rejected(3, "CHECK_LIMIT_REACHED", checks=2))
    clock.set_time(at(1000))
    assert twin.eligibility(EXP1, TXN).status is EligibilityStatus.CHECK_LIMIT_REACHED


def test_c12_answer_for_another_check_is_rejected(twin):
    ambiguous(twin)
    twin.apply_event(dispatched(3, "CHK-1"))
    before = snap(twin)
    result = twin.apply_event(answered(4, "AUTHORITATIVE_SUCCESS", check="CHK-OTHER"))
    assert result.status is ApplyStatus.INVALID_TRANSITION
    assert "does not match" in result.detail
    assert snap(twin) == before


# ============================================================ Group D — financial safety


def test_d01_authoritative_success_is_reflected(twin):
    ambiguous(twin)
    apply_all(twin, dispatched(3), answered(4, "AUTHORITATIVE_SUCCESS"))
    p = snap(twin)
    assert (p.workflow_state, p.financial_outcome, p.ledger_state_observed) == (
        "CLOSED",
        "SUCCESS",
        "CONFIRMED_SUCCESS",
    )
    assert p.terminal and p.last_evidence_reference.evidence_id == "EV-4"
    assert p.last_authoritative_evidence_at == at(101)


def test_d02_authoritative_failure_does_not_claim_a_reversal(twin):
    ambiguous(twin)
    apply_all(twin, dispatched(3), answered(4, "AUTHORITATIVE_FAILURE"))
    p = snap(twin)
    assert (p.workflow_state, p.financial_outcome) == ("CLOSED", "FAILURE")
    assert p.ledger_state_observed == "CONFIRMED_FAILED"


def reversal(version, **kw):
    return event(
        "REVERSAL_COMPLETED", version, t=150, ledger="REVERSED", outcome="FAILURE",
        evidence_reference=evidence(4), checks=1, **kw,
    )  # fmt: skip


def test_d03_reversal_is_reflected_only_from_a_backend_event(twin):
    ambiguous(twin)
    apply_all(twin, dispatched(3), answered(4, "AUTHORITATIVE_FAILURE"))
    assert snap(twin).ledger_state_observed == "CONFIRMED_FAILED"
    assert twin.apply_event(reversal(5)).applied
    assert snap(twin).ledger_state_observed == "REVERSED"


def test_d04_the_twin_exposes_no_ledger_or_money_operations():
    public = {n for n in dir(TransactionTwinRegistry) if not n.startswith("_")}
    assert public == {
        "apply_event",
        "conflicts",
        "eligibility",
        "eligible_transactions",
        "policy",
        "register_route",
        "replay",
        "snapshot",
        "transaction_ids",
    }


def test_d05_timeout_cannot_authorise_reversal(twin):
    with pytest.raises(ValidationError, match="TIMEOUT_CANNOT_IMPLY_OUTCOME"):
        timed_out(ledger="REVERSED", outcome="FAILURE", evidence_reference=evidence())
    twin.apply_event(initiated())
    forged = forge(timed_out(), ledger_state="REVERSED", financial_outcome="FAILURE")
    result = twin.apply_event(forged)
    assert result.status is ApplyStatus.REJECTED_INVALID
    assert snap(twin).ledger_state_observed == "DEBITED"


def test_d06_pending_cannot_be_followed_by_an_unconfirmed_reversal(twin):
    ambiguous(twin)
    apply_all(twin, dispatched(3), answered(4, "PENDING"))
    result = twin.apply_event(reversal(5))
    assert not result.applied
    p = snap(twin)
    assert p.ledger_state_observed == "DEBITED" and p.financial_outcome == "PENDING"


def test_d07_contradicting_verified_outcome_is_quarantined_with_evidence(twin):
    ambiguous(twin)
    apply_all(twin, dispatched(3), answered(4, "AUTHORITATIVE_SUCCESS"))
    contradicting = event(
        "PAYMENT_RESPONSE_RECEIVED", 5, t=120, ledger="CONFIRMED_FAILED", outcome="FAILURE",
        evidence_reference=evidence(5, "ORIGINAL_PAYMENT_RESPONSE"), checks=1,
    )  # fmt: skip
    result = twin.apply_event(contradicting)
    assert result.status is ApplyStatus.QUARANTINED
    assert result.error_code is ErrorCode.EVIDENCE_CONFLICT
    p = snap(twin)
    assert p.workflow_state == "QUARANTINED" and p.quarantine_reason == "EVIDENCE_CONFLICT"
    assert p.financial_outcome == "SUCCESS"  # the twin does not pick the new claim
    (record,) = twin.conflicts(EXP1, TXN)
    assert record.held_evidence_reference.evidence_id == "EV-4"
    assert record.evidence_reference.evidence_id == "EV-5"
    assert record.claimed_outcome == "FAILURE"


def test_d07b_resolved_outcome_cannot_silently_unresolve_even_across_a_gap(twin):
    ambiguous(twin)
    apply_all(twin, dispatched(3), answered(4, "AUTHORITATIVE_SUCCESS"))
    regressed = answered(7, "UNKNOWN", check="CHK-9", checks=1)  # versions 5–6 missing
    result = twin.apply_event(regressed)
    assert result.status is ApplyStatus.QUARANTINED
    assert "regressed" in result.detail
    p = snap(twin)
    assert p.quarantine_reason == "INVARIANT_VIOLATION" and p.financial_outcome == "SUCCESS"


def test_d07c_decreasing_backend_attempt_count_is_quarantined(twin):
    ambiguous(twin)
    apply_all(twin, dispatched(3, checks=2), answered(4, "UNKNOWN", checks=2))
    result = twin.apply_event(dispatched(5, "CHK-2", t=300, checks=1))
    assert result.status is ApplyStatus.QUARANTINED
    assert "decreased" in result.detail
    assert snap(twin).checks_used == 2


def test_d03b_reversal_after_a_confirmed_success_is_quarantined(twin):
    ambiguous(twin)
    apply_all(twin, dispatched(3), answered(4, "AUTHORITATIVE_SUCCESS"))
    result = twin.apply_event(reversal(5))
    assert result.status is ApplyStatus.QUARANTINED
    assert snap(twin).ledger_state_observed == "CONFIRMED_SUCCESS"


def test_d08_terminal_transaction_is_never_selected(twin, clock):
    ambiguous(twin)
    apply_all(twin, dispatched(3), answered(4, "AUTHORITATIVE_SUCCESS"))
    clock.set_time(at(1000))
    assert twin.eligibility(EXP1, TXN).status is EligibilityStatus.TERMINAL
    assert twin.eligible_transactions(EXP1, ROUTE) == ()


def test_d09_duplicate_reversal_has_no_repeated_effect(twin):
    ambiguous(twin)
    apply_all(twin, dispatched(3), answered(4, "AUTHORITATIVE_FAILURE"), reversal(5))
    before = snap(twin)
    assert twin.apply_event(reversal(5)).status is ApplyStatus.DUPLICATE
    assert twin.apply_event(reversal(5, event_id="redelivered")).status is ApplyStatus.DUPLICATE
    assert snap(twin) == before and twin.conflicts(EXP1, TXN) == ()


def test_d10_missing_evidence_cannot_establish_finality(twin):
    ambiguous(twin)
    with pytest.raises(ValidationError, match="EVIDENCE_REQUIRED"):
        answered(3, "AUTHORITATIVE_SUCCESS", evidence_reference=None)
    forged = forge(answered(3, "AUTHORITATIVE_SUCCESS"), evidence_reference=None)
    assert twin.apply_event(forged).status is ApplyStatus.REJECTED_INVALID
    assert snap(twin).financial_outcome == "UNKNOWN"


# ============================================================ Group E — ordering


def test_e01_new_version_applies(twin):
    assert [r.status for r in apply_all(twin, initiated(), timed_out())] == [
        ApplyStatus.APPLIED,
        ApplyStatus.APPLIED,
    ]
    assert snap(twin).version == 2


def test_e02_same_event_id_redelivery_is_a_duplicate(twin):
    ambiguous(twin)
    result = twin.apply_event(timed_out())
    assert result.status is ApplyStatus.DUPLICATE
    assert result.error_code is ErrorCode.DUPLICATE_EVENT


def test_e03_same_event_id_with_different_content_is_a_conflict(twin):
    ambiguous(twin)
    result = twin.apply_event(timed_out(t=16))  # same default event_id, different content
    assert result.status is ApplyStatus.CONFLICT
    assert result.error_code is ErrorCode.CONFLICTING_EVENT
    assert snap(twin).quarantine_reason == "CONFLICTING_EVENT"


def test_e04_lower_unapplied_version_is_stale(twin):
    twin.apply_event(initiated())
    assert twin.apply_event(timed_out(version=3)).version_gap == 1
    result = twin.apply_event(event("STATUS_CHECK_REJECTED", 2, check_id="C0",
                                    result_code="RATE_LIMITED"))  # fmt: skip
    assert result.status is ApplyStatus.STALE_VERSION
    assert result.error_code is ErrorCode.STALE_EVENT_VERSION
    assert snap(twin).version == 3


def test_e05_same_version_with_different_content_is_a_conflict(twin):
    ambiguous(twin)
    twin.apply_event(dispatched(3, "C1"))
    other = dispatched(3, "C2", event_id="different-delivery")
    result = twin.apply_event(other)
    assert result.status is ApplyStatus.CONFLICT
    p = snap(twin)
    assert p.workflow_state == "QUARANTINED" and p.last_check_id == "C1"


def test_e06_version_gap_is_reported(twin):
    twin.apply_event(initiated())
    result = twin.apply_event(timed_out(version=4))
    assert result.applied and result.version_gap == 2
    assert "2 version(s) missing" in result.detail


def test_e07_out_of_order_history_never_regresses_state(twin):
    twin.apply_event(initiated())
    early = twin.apply_event(dispatched(3))  # arrives before the timeout it depends on
    assert early.status is ApplyStatus.INVALID_TRANSITION
    assert snap(twin).workflow_state == "ACTIVE"
    assert twin.apply_event(timed_out()).applied
    assert twin.apply_event(dispatched(3)).applied  # redelivery now succeeds
    assert twin.apply_event(timed_out()).status is ApplyStatus.DUPLICATE
    assert snap(twin).workflow_state == "CHECK_IN_FLIGHT" and snap(twin).version == 3


def test_e08_wrong_experiment_cannot_mutate_the_projection(twin):
    ambiguous(twin)
    result = twin.apply_event(dispatched(3, exp=EXP2))
    assert result.status is ApplyStatus.UNKNOWN_TRANSACTION
    assert snap(twin).workflow_state == "AMBIGUOUS"


def test_e09_wrong_transaction_cannot_mutate_the_projection(twin):
    ambiguous(twin)
    result = twin.apply_event(dispatched(3, txn="TXN-OTHER"))
    assert result.status is ApplyStatus.UNKNOWN_TRANSACTION
    assert snap(twin).version == 2
    with pytest.raises(UnknownTransactionError):
        twin.snapshot(EXP1, "TXN-OTHER")


HISTORY = [
    initiated(),
    timed_out(),
    dispatched(3),
    answered(4, "PENDING"),
    answered(4, "PENDING"),
    dispatched(6, "C2", t=200, checks=2, outcome="PENDING", evidence_reference=evidence(4)),
    initiated(txn="TXN2"),
    timed_out(txn="TXN2"),
    check_timed_out(7, "C2", t=211, checks=2, outcome="PENDING", evidence_reference=evidence(4)),
]


def _replay_run():
    clock = ManualClock(at(0))
    registry = TransactionTwinRegistry(make_policy(), clock)
    results = []
    for e in HISTORY:
        clock.advance(7)
        results.append(registry.apply_event(e))
    clock.set_time(at(400))
    return results, [registry.snapshot(EXP1, t) for t in (TXN, "TXN2")]


def test_e10_replay_is_deterministic():
    (r1, s1), (r2, s2) = _replay_run(), _replay_run()
    assert r1 == r2
    assert [s.model_dump_json() for s in s1] == [s.model_dump_json() for s in s2]
    assert [r.status for r in r1] == [
        ApplyStatus.APPLIED,
        ApplyStatus.APPLIED,
        ApplyStatus.APPLIED,
        ApplyStatus.APPLIED,
        ApplyStatus.DUPLICATE,
        ApplyStatus.APPLIED,
        ApplyStatus.APPLIED,
        ApplyStatus.APPLIED,
        ApplyStatus.APPLIED,
    ]


def test_e11_concurrent_duplicate_delivery_applies_once(twin):
    twin.apply_event(initiated())
    incoming = timed_out()
    barrier = threading.Barrier(32)

    def deliver(_):
        barrier.wait()
        return twin.apply_event(incoming).status

    with ThreadPoolExecutor(max_workers=32) as pool:
        statuses = list(pool.map(deliver, range(32)))
    assert statuses.count(ApplyStatus.APPLIED) == 1
    assert statuses.count(ApplyStatus.DUPLICATE) == 31


def test_e12_concurrent_conflicting_outcomes_cannot_both_win(twin, monkeypatch):
    """Force both deliveries to meet inside application; only the lock keeps them apart."""

    import intelligence.twin.transaction_twin as module

    ambiguous(twin)
    twin.apply_event(dispatched(3))
    original = module.transition
    rendezvous = threading.Barrier(2, timeout=0.5)

    def transition_after_rendezvous(state, event, gap):
        try:
            rendezvous.wait()
        except threading.BrokenBarrierError:
            pass
        return original(state, event, gap)

    monkeypatch.setattr(module, "transition", transition_after_rendezvous)
    success = answered(4, "AUTHORITATIVE_SUCCESS", event_id="answer-success")
    failure = answered(4, "AUTHORITATIVE_FAILURE", event_id="answer-failure")
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(twin.apply_event, [success, failure]))
    assert sorted(r.status for r in statuses) == sorted([ApplyStatus.APPLIED, ApplyStatus.CONFLICT])
    assert snap(twin).workflow_state == "QUARANTINED"


# ============================================================ Group F — eligibility


@pytest.mark.parametrize(
    ("reference", "first_eligible"), [("INITIATED", 90.0), ("TIMED_OUT", 105.0)]
)
def test_f01_f04_first_check_reference_point(clock, reference, first_eligible):
    registry = TransactionTwinRegistry(make_policy(first_check_reference=reference), clock)
    ambiguous(registry)
    clock.set_time(at(first_eligible - 0.001))
    early = registry.eligibility(EXP1, TXN)
    assert early.status is EligibilityStatus.NOT_YET_ELIGIBLE
    assert early.eligible_at == at(first_eligible)
    assert snap(registry).next_eligible_at == at(first_eligible)
    clock.set_time(at(first_eligible))
    assert registry.eligibility(EXP1, TXN).status is EligibilityStatus.ELIGIBLE


def test_f05_minimum_spacing_is_enforced(twin, clock):
    ambiguous(twin)
    apply_all(twin, dispatched(3, t=100), answered(4, "UNKNOWN", t=101))
    clock.set_time(at(189.999))
    assert twin.eligibility(EXP1, TXN).status is EligibilityStatus.NOT_YET_ELIGIBLE
    clock.set_time(at(190))
    assert twin.eligibility(EXP1, TXN).status is EligibilityStatus.ELIGIBLE


def test_f06_maximum_counted_checks_is_enforced(clock):
    registry = TransactionTwinRegistry(make_policy(max_counted_checks=1), clock)
    ambiguous(registry)
    apply_all(registry, dispatched(3), answered(4, "UNKNOWN"))
    clock.set_time(at(5000))
    assert registry.eligibility(EXP1, TXN).status is EligibilityStatus.CHECK_LIMIT_REACHED


def test_f07_pre_dispatch_rejections_are_not_counted_locally(twin, clock):
    ambiguous(twin)
    for version, code in ((3, "RATE_LIMITED"), (4, "CREDIT_EXHAUSTED"), (5, "RATE_LIMITED")):
        twin.apply_event(rejected(version, code, check=f"R{version}", checks=0))
    clock.set_time(at(200))
    assert snap(twin).checks_used == 0
    assert twin.eligibility(EXP1, TXN).status is EligibilityStatus.ELIGIBLE


def test_f08_f10_unresolvable_states_are_ineligible(clock):
    cases = {
        EligibilityStatus.TERMINAL: [dispatched(3), answered(4, "AUTHORITATIVE_SUCCESS")],
        EligibilityStatus.QUARANTINED: [timed_out(t=16)],
        EligibilityStatus.CHECK_IN_FLIGHT: [dispatched(3)],
    }
    for expected, extra in cases.items():
        registry = TransactionTwinRegistry(make_policy(), ManualClock(at(0)))
        ambiguous(registry)
        apply_all(registry, *extra)
        assert registry.eligibility(EXP1, TXN, at_time=at(1000)).status is expected


def test_f11_missing_timing_evidence_fails_closed(twin):
    twin.apply_event(initiated())
    twin.apply_event(timed_out(checks=1))  # backend counts a check the twin never saw
    verdict = twin.eligibility(EXP1, TXN, at_time=at(1000))
    assert verdict.status is EligibilityStatus.INDETERMINATE
    assert "no check time" in verdict.reason


def test_f11b_unevaluable_stop_codes_fail_closed(clock):
    registry = TransactionTwinRegistry(make_policy(stop_error_codes=["U16"]), clock)
    ambiguous(registry)
    verdict = registry.eligibility(EXP1, TXN, at_time=at(1000))
    assert verdict.status is EligibilityStatus.INDETERMINATE
    assert "stop codes" in verdict.reason


def test_f12_check_window_is_anchored_and_closes(clock):
    registry = TransactionTwinRegistry(make_policy(check_window_s=300.0), clock)
    ambiguous(registry)
    assert registry.eligibility(EXP1, TXN, at_time=at(300)).status is EligibilityStatus.ELIGIBLE
    closed = registry.eligibility(EXP1, TXN, at_time=at(300.001))
    assert closed.status is EligibilityStatus.CHECK_WINDOW_CLOSED
    apply_all(registry, dispatched(3, t=250), answered(4, "UNKNOWN", t=251))
    # Spacing would push the next check past the window: no further check is offered.
    late = registry.eligibility(EXP1, TXN, at_time=at(260))
    assert late.status is EligibilityStatus.CHECK_WINDOW_CLOSED


def test_f13_research_timing_is_labelled_distinct_from_verified(clock):
    research = TransactionTwinRegistry(make_policy(), clock)
    ambiguous(research)
    r = research.eligibility(EXP1, TXN, at_time=at(100))
    assert (r.policy_regime, r.policy_verification) == ("RESEARCH_SCALED", "RESEARCH_PARAMETER")
    assert r.deadline_verification == "RESEARCH_PARAMETER"
    verified = TransactionTwinRegistry(
        make_policy(policy_id="tier1", regime="TIER1_COMPLIANT", verification_status="VERIFIED"),
        clock,
    )
    ambiguous(verified)
    v = verified.eligibility(EXP1, TXN, at_time=at(100))
    assert (v.policy_regime, v.policy_verification) == ("TIER1_COMPLIANT", "VERIFIED")
    # The deadline was set under a different policy, so its basis is unknown, not VERIFIED.
    assert v.deadline_verification is None


# ============================================================ Group G — deadlines


def test_g01_deadline_is_stored_from_the_event(twin):
    ambiguous(twin)
    p = snap(twin)
    assert p.deadline_at == at(DEADLINE) and p.deadline_policy_id == POLICY_ID


def test_g02_g08_missing_deadline_stays_missing(twin, clock):
    apply_all(twin, initiated(), timed_out(deadline=None))
    clock.set_time(at(10 * 86_400))
    p = snap(twin)
    assert p.deadline_at is None and p.deadline_policy_id is None
    assert p.deadline_escalated is False and p.workflow_state == "AMBIGUOUS"
    assert twin.eligibility(EXP1, TXN).deadline_at is None


def test_g03_escalation_starts_at_the_configured_threshold(twin, clock):
    ambiguous(twin)
    clock.set_time(at(DEADLINE - 600.001))
    assert snap(twin).workflow_state == "AMBIGUOUS"
    clock.set_time(at(DEADLINE - 600))
    p = snap(twin)
    assert p.workflow_state == "ESCALATED" and p.deadline_escalated
    assert p.version == 2  # clock-derived; the backend version is unchanged


def test_g04_escalation_never_overrides_the_attempt_limit(clock):
    tier2 = make_policy(
        regime="TIER2_EXPERIMENTAL", escalation_extra_checks=2, max_counted_checks=1
    )
    registry = TransactionTwinRegistry(tier2, clock)
    ambiguous(registry)
    apply_all(registry, dispatched(3), answered(4, "UNKNOWN"))
    clock.set_time(at(DEADLINE - 1))
    verdict = registry.eligibility(EXP1, TXN)
    assert verdict.deadline_escalated
    assert verdict.status is EligibilityStatus.CHECK_LIMIT_REACHED


def test_g05_escalation_survives_a_non_final_check(twin, clock):
    ambiguous(twin)
    clock.set_time(at(3100))
    twin.apply_event(dispatched(3, t=3100))
    in_flight = snap(twin)
    assert in_flight.workflow_state == "CHECK_IN_FLIGHT" and in_flight.deadline_escalated
    twin.apply_event(answered(4, "PENDING", t=3101))
    assert snap(twin).workflow_state == "ESCALATED"


def test_g06_resolution_clears_escalation_and_eligibility(twin, clock):
    ambiguous(twin)
    clock.set_time(at(3100))
    apply_all(twin, dispatched(3, t=3100), answered(4, "AUTHORITATIVE_SUCCESS", t=3101))
    p = snap(twin)
    assert p.workflow_state == "CLOSED" and not p.deadline_escalated
    assert p.next_eligible_at is None
    assert twin.eligibility(EXP1, TXN).status is EligibilityStatus.TERMINAL


def test_g07_deadline_passage_triggers_no_financial_change(twin, clock):
    ambiguous(twin)
    clock.set_time(at(DEADLINE + 86_400))
    p = snap(twin)
    assert p.ledger_state_observed == "DEBITED" and p.financial_outcome == "UNKNOWN"
    assert p.workflow_state == "ESCALATED" and p.version == 2
    assert twin.conflicts(EXP1, TXN) == ()


# ============================================================ Group H — queries, replay


def _populate(registry):
    for txn in ("T-3", "T-1", "T-2"):
        apply_all(registry, initiated(txn=txn), timed_out(txn=txn))
    apply_all(registry, initiated(txn="T-0", t=-50), timed_out(txn="T-0", t=-30))
    apply_all(registry, initiated(txn="T-C", route="A1-C", beneficiary="C"))
    apply_all(registry, timed_out(txn="T-C", route="A1-C", beneficiary="C"))
    apply_all(registry, initiated(txn="T-X", exp=EXP2), timed_out(txn="T-X", exp=EXP2))


def test_h01_h03_query_filters_and_orders_deterministically(twin):
    _populate(twin)
    ids = [e.transaction_id for e in twin.eligible_transactions(EXP1, ROUTE, at_time=at(500))]
    assert ids == ["T-0", "T-1", "T-2", "T-3"]  # T-0 eligible first, then by id
    assert [e.transaction_id for e in twin.eligible_transactions(EXP1, "A1-C", at(500))] == ["T-C"]
    assert [e.transaction_id for e in twin.eligible_transactions(EXP2, ROUTE, at(500))] == ["T-X"]


def test_h04_h06_resolved_handoff_and_quarantined_are_excluded(twin):
    _populate(twin)
    apply_all(
        twin,
        dispatched(3, txn="T-1"),
        answered(4, "AUTHORITATIVE_SUCCESS", txn="T-1"),
        event("RECONCILIATION_HANDOFF", 3, txn="T-2", ledger="RECONCILIATION_PENDING", t=400),
        timed_out(txn="T-3", t=99),  # same event_id, different content: quarantined
    )
    ids = [e.transaction_id for e in twin.eligible_transactions(EXP1, ROUTE, at_time=at(500))]
    assert ids == ["T-0"]


def test_h07_replay_rebuilds_an_identical_projection(clock):
    first = TransactionTwinRegistry(make_policy(), clock)
    second = TransactionTwinRegistry(make_policy(), clock)
    first.replay(HISTORY)
    second.replay(HISTORY)
    assert snap(first).model_dump_json() == snap(second).model_dump_json()


def test_h08_duplicate_replay_has_no_additional_effect(twin):
    twin.replay(HISTORY)
    before = [snap(twin, t) for t in (TXN, "TXN2")]
    again = twin.replay(HISTORY)
    assert {r.status for r in again} == {ApplyStatus.DUPLICATE}
    assert [snap(twin, t) for t in (TXN, "TXN2")] == before


def test_h09_previous_snapshots_are_immutable(twin):
    ambiguous(twin)
    earlier = snap(twin)
    apply_all(twin, dispatched(3), answered(4, "AUTHORITATIVE_SUCCESS"))
    assert earlier.workflow_state == "AMBIGUOUS" and earlier.version == 2
    with pytest.raises(ValidationError, match="frozen"):
        earlier.version = 99


def test_h10_independent_registries_share_no_state(clock):
    first = TransactionTwinRegistry(make_policy(), clock)
    second = TransactionTwinRegistry(make_policy(), clock)
    ambiguous(first)
    assert second.transaction_ids(EXP1) == ()
    assert second.apply_event(initiated()).applied  # not a duplicate of first's event


def test_h11_handoff_then_resolution_closes(twin):
    ambiguous(twin)
    handoff = event("RECONCILIATION_HANDOFF", 3, ledger="RECONCILIATION_PENDING", t=500)
    resolved = event(
        "RECONCILIATION_RESOLVED", 4, ledger="CONFIRMED_FAILED", outcome="FAILURE", t=900,
        evidence_reference=evidence(9, "RECONCILIATION_RECORD"),
    )  # fmt: skip
    assert twin.apply_event(handoff).applied
    assert twin.eligibility(EXP1, TXN).status is EligibilityStatus.IN_RECONCILIATION
    assert twin.apply_event(resolved).applied
    p = snap(twin)
    assert (p.workflow_state, p.financial_outcome, p.ledger_state_observed) == (
        "CLOSED",
        "FAILURE",
        "CONFIRMED_FAILED",
    )


def test_h12_quarantine_ends_only_through_reconciliation(twin):
    ambiguous(twin)
    apply_all(twin, dispatched(3), answered(4, "AUTHORITATIVE_SUCCESS"))
    conflict = event(
        "EVIDENCE_CONFLICT_RECORDED", 5, ledger="CONFIRMED_SUCCESS", outcome="SUCCESS",
        evidence_reference=evidence(4), checks=1, t=130,
    )  # fmt: skip
    assert twin.apply_event(conflict).applied
    assert snap(twin).quarantine_reason == "EVIDENCE_CONFLICT"
    blocked = twin.apply_event(event("TRANSACTION_CLOSED", 6, ledger="CLOSED", outcome="SUCCESS",
                                     evidence_reference=evidence(4), checks=1))  # fmt: skip
    assert blocked.status is ApplyStatus.QUARANTINED
    resolved = event(
        "RECONCILIATION_RESOLVED", 6, ledger="CONFIRMED_SUCCESS", outcome="SUCCESS", t=900,
        evidence_reference=evidence(9, "RECONCILIATION_RECORD"), checks=1,
        event_id="resolution",
    )  # fmt: skip
    assert twin.apply_event(resolved).applied
    p = snap(twin)
    assert p.workflow_state == "CLOSED" and p.quarantine_reason is None


# ============================================================ Group I — robustness


@pytest.mark.parametrize(
    "build",
    [
        lambda: TransactionTwinConfig(unexpected=True),
        lambda: TransactionTwinConfig(require_registered_routes="yes"),
    ],
)
def test_i01_invalid_configuration_is_rejected(build):
    with pytest.raises(ValidationError):
        build()


def test_i01b_wrong_dependency_types_are_rejected(clock):
    with pytest.raises(TypeError):
        TransactionTwinRegistry({"policy_id": "x"}, clock)
    with pytest.raises(TypeError):
        TransactionTwinRegistry(make_policy(), clock, config={"require_registered_routes": True})


def test_i02_naive_clock_and_query_times_are_rejected(clock):
    class NaiveClock:
        def now(self):
            return datetime(2026, 3, 1, 12, 0)

    with pytest.raises(TransactionTwinInvariantError, match="timezone-aware"):
        TransactionTwinRegistry(make_policy(), NaiveClock()).apply_event(initiated())
    registry = TransactionTwinRegistry(make_policy(), clock)
    ambiguous(registry)
    with pytest.raises(TransactionTwinInvariantError, match="timezone-aware"):
        registry.eligible_transactions(EXP1, ROUTE, at_time=datetime(2026, 3, 1, 13, 0))


def test_i03_backward_clock_movement_is_rejected():
    class RegressingClock:
        def __init__(self):
            self.times = [at(10), at(9)]

        def now(self):
            return self.times.pop(0)

    registry = TransactionTwinRegistry(make_policy(), RegressingClock())
    registry.apply_event(initiated())
    with pytest.raises(TransactionTwinInvariantError, match="backwards"):
        registry.snapshot(EXP1, TXN)


def test_i04_invalid_transition_leaves_state_intact(twin):
    ambiguous(twin)
    before = snap(twin)
    impossible = (
        timed_out(version=3, t=20),  # a second payment timeout
        event(
            "RECONCILIATION_RESOLVED",
            3,
            ledger="CONFIRMED_SUCCESS",
            outcome="SUCCESS",
            evidence_reference=evidence(3, "RECONCILIATION_RECORD"),
            event_id="resolved",
        ),  # resolution without a handoff
    )
    for bad in impossible:
        assert twin.apply_event(bad).status is ApplyStatus.INVALID_TRANSITION
    assert snap(twin) == before


def test_i04b_backend_invariant_violations_quarantine_without_adopting_facts(twin):
    ambiguous(twin)
    skipped_confirmation = event(
        "TRANSACTION_CLOSED", 3, ledger="CLOSED", outcome="SUCCESS", evidence_reference=evidence()
    )
    result = twin.apply_event(skipped_confirmation)
    assert result.status is ApplyStatus.QUARANTINED
    p = snap(twin)
    assert p.quarantine_reason == "INVARIANT_VIOLATION"
    assert (p.ledger_state_observed, p.financial_outcome, p.version) == ("DEBITED", "UNKNOWN", 2)


def test_i05_snapshot_failure_publishes_nothing(twin, monkeypatch):
    import intelligence.twin.transaction_twin as module

    ambiguous(twin)
    before = snap(twin)
    original = module._build_projection

    def failing_for_version_3(state, policy, now):
        if state.version == 3:
            TransactionProjection.model_validate({})  # raises a real ValidationError
        return original(state, policy, now)

    monkeypatch.setattr(module, "_build_projection", failing_for_version_3)
    result = twin.apply_event(dispatched(3))
    assert result.status is ApplyStatus.INVALID_TRANSITION
    monkeypatch.undo()
    assert snap(twin) == before
    assert twin.apply_event(dispatched(3)).applied  # nothing was half-recorded


def test_i06_concurrent_readers_see_consistent_projections(clock):
    registry = TransactionTwinRegistry(make_policy(), clock)
    registry.apply_event(initiated(txn="T-READ"))
    done = threading.Event()
    seen: list[int] = []

    def write():
        for n in range(60):
            apply_all(registry, initiated(txn=f"T{n}"), timed_out(txn=f"T{n}"))

    def read():
        while not done.is_set():
            listed = registry.eligible_transactions(EXP1, ROUTE, at_time=at(500))
            seen.append(len(listed))
            registry.snapshot(EXP1, "T-READ")

    with ThreadPoolExecutor(max_workers=1) as readers:
        reader = readers.submit(read)
        write()
        done.set()
        reader.result()  # re-raises any inconsistency the reader hit
    assert seen == sorted(seen)
    assert len(registry.eligible_transactions(EXP1, ROUTE, at_time=at(500))) == 60


def test_i07_unexpected_event_type_is_rejected_explicitly(twin):
    twin.apply_event(initiated())
    result = twin.apply_event(forge(timed_out(), event_type="BOGUS_TYPE"))
    assert result.status is ApplyStatus.REJECTED_INVALID
    assert result.error_code is ErrorCode.SCHEMA_INVALID
    assert snap(twin).version == 1


def test_i08_event_for_unknown_transaction_creates_nothing(twin):
    result = twin.apply_event(timed_out())
    assert result.status is ApplyStatus.UNKNOWN_TRANSACTION
    assert result.error_code is ErrorCode.UNKNOWN_TRANSACTION
    assert result.projection_version is None
    assert twin.transaction_ids(EXP1) == ()


def test_i09_i10_no_backend_or_downstream_imports():
    code = (
        "import sys, intelligence.twin\n"
        "bad = sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'backend', 'sqlalchemy', 'psycopg', 'boto3', 'httpx', 'requests'} "
        "or m.startswith(('intelligence.controller', 'intelligence.ml', "
        "'intelligence.simulation')))\n"
        "print(','.join(bad))\n"
    )
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and constant code
        [sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    assert completed.stdout.strip() == ""


def test_i11_pure_eligibility_matches_the_registry(twin, clock):
    ambiguous(twin)
    clock.set_time(at(95))
    projection = snap(twin)
    assert evaluate_eligibility(projection, twin.policy, at(95)) == twin.eligibility(EXP1, TXN)
    with pytest.raises(TransactionTwinInvariantError):
        evaluate_eligibility(projection, twin.policy, datetime(2026, 3, 1))
