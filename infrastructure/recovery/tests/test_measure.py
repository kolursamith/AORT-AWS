"""The measurement maths: RTO, availability and RPO.

These numbers are the project's headline results, so the arithmetic is pinned
down exactly - including the cases where the honest answer is "unknown".
"""

from __future__ import annotations

import pytest

from aort_recovery.measure import (
    Probe,
    availability_ratio,
    rows_lost,
    rpo_seconds,
    time_to_recover,
    transaction_integrity,
)

FAILURE_AT = 1000.0


def probes(*pairs):
    return [Probe(at=at, ok=ok) for at, ok in pairs]


# --- availability ---------------------------------------------------------------

def test_availability_is_the_share_of_successful_probes():
    assert availability_ratio(probes((1, True), (2, False), (3, True), (4, True))) == 0.75


@pytest.mark.parametrize("sequence,expected", [
    ([(1, True)], 1.0),
    ([(1, False)], 0.0),
])
def test_availability_extremes(sequence, expected):
    assert availability_ratio(probes(*sequence)) == expected


def test_availability_without_probes_is_an_error_not_one():
    # Claiming 100% availability because nothing was measured would be a lie.
    with pytest.raises(ValueError):
        availability_ratio([])


# --- RTO --------------------------------------------------------------------------

def test_rto_is_measured_to_the_first_sustained_recovery():
    timeline = probes((1001, False), (1002, False), (1010, True), (1011, True))
    assert time_to_recover(timeline, FAILURE_AT) == 10.0


def test_a_single_lucky_probe_does_not_count_as_recovered():
    # One success between failures is a flap, not a recovery.
    timeline = probes((1005, True), (1006, False), (1020, True), (1021, True))
    assert time_to_recover(timeline, FAILURE_AT) == 20.0


def test_rto_is_none_when_the_service_never_comes_back():
    timeline = probes((1001, False), (1002, False), (1003, False))
    assert time_to_recover(timeline, FAILURE_AT) is None


def test_rto_is_none_when_recovery_is_never_sustained():
    timeline = probes((1001, True), (1002, False), (1003, True), (1004, False))
    assert time_to_recover(timeline, FAILURE_AT) is None


def test_probes_before_the_failure_are_ignored():
    timeline = probes((990, True), (991, True), (1005, False), (1010, True), (1011, True))
    assert time_to_recover(timeline, FAILURE_AT) == 10.0


def test_required_consecutive_successes_is_configurable():
    timeline = probes((1010, True), (1011, True), (1012, True))
    assert time_to_recover(timeline, FAILURE_AT, consecutive_ok=3) == 10.0
    assert time_to_recover(timeline, FAILURE_AT, consecutive_ok=4) is None


def test_rto_is_never_negative_even_with_clock_jitter():
    timeline = probes((999.5, True), (1000.5, True), (1001, True))
    assert time_to_recover(timeline, FAILURE_AT) == 0.0


def test_unordered_probes_are_handled():
    timeline = probes((1011, True), (1001, False), (1010, True))
    assert time_to_recover(timeline, FAILURE_AT) == 10.0


# --- RPO --------------------------------------------------------------------------

def test_rpo_is_the_gap_between_the_newest_backup_and_the_failure():
    assert rpo_seconds(failure_at=1000.0, backup_at=940.0) == 60.0


def test_rpo_without_a_backup_is_unknown_not_zero():
    # No backup does not mean no data loss; it means it cannot be bounded.
    assert rpo_seconds(failure_at=1000.0, backup_at=None) is None


def test_rpo_is_clamped_when_a_backup_lands_after_the_failure():
    assert rpo_seconds(failure_at=1000.0, backup_at=1005.0) == 0.0


# --- ledger ------------------------------------------------------------------------

def test_rows_lost_counts_only_what_disappeared():
    before = {"m_client": 12, "m_loan": 4}
    after = {"m_client": 10, "m_loan": 4}
    assert rows_lost(before, after) == 2


def test_rows_gained_after_recovery_is_not_negative_loss():
    # New activity after recovery must not mask lost rows.
    assert rows_lost({"m_client": 10}, {"m_client": 14}) == 0


def test_rows_lost_is_unknown_when_a_table_could_not_be_counted():
    assert rows_lost({"m_client": 10}, {}) is None
    assert rows_lost({}, {"m_client": 10}) is None


def test_ledger_integrity_compares_debits_and_credits():
    assert transaction_integrity(debits=1500.0, credits=1500.0) == "balanced"
    assert transaction_integrity(debits=1500.0, credits=1499.0) == "unbalanced"
    assert transaction_integrity(debits=None, credits=1500.0) == "unknown"


def test_ledger_integrity_tolerates_floating_point_noise():
    assert transaction_integrity(debits=1500.0000001, credits=1500.0) == "balanced"
