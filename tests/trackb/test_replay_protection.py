"""E. Replay tests (sequential + concurrent) through the real signed HTTP surface."""

from __future__ import annotations

import pytest

from backend.aorts.security.replay_simulator import ReplaySimulator
from tests.trackb.conftest import ADMIN_TOKEN


@pytest.fixture(params=[True, False], ids=["auto_reverse", "explicit_reverse"])
def results(request, stack_factory):
    st = stack_factory(auto_reverse_on_confirmed_failure=request.param)
    sim = ReplaySimulator(st.client, st.db, ADMIN_TOKEN)
    records = {r["case"]: r for r in sim.run_all()}
    verify = st.ledger.verify_ledger()
    return records, verify, request.param


def test_ledger_integrity_after_all_replays(results):
    _, verify, _ = results
    assert verify["ok"], verify


def test_sequential_duplicate(results):
    r, _, auto = results
    assert r["1_valid_response_once"]["http_status"] == 200
    dup = r["2_same_response_twice"]
    assert dup["http_status"] == 409 and dup["decision"] == "DUPLICATE_EVENT"
    assert dup["actual_version"] == r["1_valid_response_once"]["actual_version"]


def test_stale_version_and_stale_timestamp(results):
    r, _, auto = results
    for case in ("3_stale_version", "3b_stale_timestamp"):
        assert r[case]["http_status"] == 409 and r[case]["decision"] == "STALE_VERSION"
        assert r[case]["state"] == "AMBIGUOUS" and r[case]["ledger_postings"] == 1  # only the debit


def test_repeated_reversal_posts_once(results):
    r, _, auto = results
    for i in (1, 2, 3):
        assert r[f"4_same_reversal_delivery_{i}"]["reversal_postings"] == 1
    statuses = [r[f"4_same_reversal_delivery_{i}"]["http_status"] for i in (1, 2, 3)]
    assert (
        statuses.count(200) == 3
    )  # first ACCEPTED (or DUPLICATE if auto-reversed), rest DUPLICATE
    assert r["4b_reversal_new_operation_id"]["http_status"] == 409
    assert r["4b_reversal_new_operation_id"]["reversal_postings"] == 1
    # Per-transaction conservation (sender net == 0 after exactly one reversal) is
    # checked for every transaction by test_ledger_integrity_after_all_replays.


def test_concurrent_duplicate_events_apply_once(results):
    r, _, auto = results
    rec = r["5_concurrent_duplicate_events"]
    assert rec["accepted_count"] == 1, rec["status_codes"]
    # Auto mode reverses on the single accepted FAILED event; explicit mode waits for a reversal request.
    assert rec["reversal_postings"] == (1 if auto else 0)
    assert rec["state"] == ("REVERSED" if auto else "CONFIRMED_FAILED")


def test_concurrent_reversals_post_once(results):
    r, _, auto = results
    rec = r["6_concurrent_reversals"]
    assert rec["reversal_postings"] == 1, rec
    assert rec["final_outcome"] == "REVERSED"


def test_duplicate_after_close(results):
    r, _, auto = results
    agreeing = r["7_duplicate_after_close_agreeing"]
    assert agreeing["http_status"] == 200 and agreeing["decision"] == "DUPLICATE"
    conflicting = r["7b_conflicting_after_close"]
    assert conflicting["http_status"] == 409
    assert conflicting["final_outcome"] == (
        "REVERSED" if auto else "FAILURE"
    )  # unchanged by the conflict
    assert conflicting["reversal_postings"] == (1 if auto else 0)


def test_wrong_transaction_and_wrong_signer(results):
    r, _, auto = results
    assert r["8_response_for_wrong_transaction"]["decision"] == "WRONG_TRANSACTION"
    assert r["8_response_for_wrong_transaction"]["state"] == "AMBIGUOUS"
    assert r["8b_event_signed_by_non_switch"]["http_status"] == 403


def test_invalid_idempotency_keys(results):
    r, _, auto = results
    assert r["9_missing_idempotency_key"]["decision"] == "VALIDATION_ERROR"
    assert r["9b_invalid_idempotency_key"]["decision"] == "VALIDATION_ERROR"


def test_forged_and_replayed_requests(results):
    r, _, auto = results
    assert r["10a_captured_request_first_use"]["http_status"] == 200
    assert r["10b_repeated_nonce"]["decision"] == "AUTH_REPLAYED_NONCE"
    assert r["10c_expired_timestamp"]["decision"] == "AUTH_EXPIRED_TIMESTAMP"
    assert r["10d_tampered_body"]["decision"] == "AUTH_INVALID_SIGNATURE"
    assert r["10e_unsigned"]["decision"] == "AUTH_MISSING"
    for case in ("10c_expired_timestamp", "10d_tampered_body", "10e_unsigned"):
        assert r[case]["state"] == "AMBIGUOUS" and r[case]["ledger_postings"] == 1
