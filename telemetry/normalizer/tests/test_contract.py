"""The I1 contract itself: what it accepts and, more importantly, rejects."""

from __future__ import annotations

import pytest

VALID_OK = {
    "schema_version": "i1-provisional-0.1",
    "snapshot_id": "0b0d6a5e-3f2a-4c55-9a7e-2f1c6d7e8a90",
    "observed_at": "2026-09-15T16:45:00.123Z",
    "component_id": "postgres",
    "signal": "commit_rate",
    "category": "database",
    "value": 12.5,
    "unit": "transactions_per_second",
    "quality": "ok",
    "source": "prometheus:job=postgres",
    "dimensions": {},
}

REQUIRED = list(VALID_OK)


def errors(validator, record):
    return list(validator.iter_errors(record))


def test_schema_is_a_valid_draft_2020_12_schema(validator):
    assert validator is not None  # check_schema ran in the fixture


def test_valid_ok_record_is_accepted(validator):
    assert not errors(validator, VALID_OK)


def test_missing_observation_with_null_value_is_accepted(validator):
    record = {**VALID_OK, "value": None, "quality": "missing"}
    assert not errors(validator, record)


def test_out_of_bounds_keeps_its_real_value(validator):
    record = {**VALID_OK, "value": 1.5, "quality": "out_of_bounds"}
    assert not errors(validator, record)


@pytest.mark.parametrize("quality", ["ok", "out_of_bounds"])
def test_qualities_that_carry_a_value_reject_null(validator, quality):
    record = {**VALID_OK, "value": None, "quality": quality}
    assert errors(validator, record)


@pytest.mark.parametrize("quality", ["missing", "non_finite", "ambiguous"])
def test_qualities_without_a_value_reject_a_number(validator, quality):
    # Guards the no-fabrication rule: an absent signal may not carry a value.
    record = {**VALID_OK, "value": 0.0, "quality": quality}
    assert errors(validator, record)


@pytest.mark.parametrize(
    "field,bad",
    [
        ("schema_version", "i1-provisional-9.9"),
        ("component_id", "mainframe"),
        ("category", "network"),
        ("unit", "furlongs"),
        ("quality", "good"),
        ("signal", "CommitRate"),
        ("signal", "1rate"),
        ("signal", ""),
        ("signal", "x" * 65),
        ("observed_at", "2026-09-15 16:45:00"),
        ("observed_at", "2026-09-15T16:45:00Z"),
        ("observed_at", "2026-09-15T16:45:00.123+05:30"),
        ("snapshot_id", "not-a-uuid"),
        ("snapshot_id", "0b0d6a5e-3f2a-1c55-9a7e-2f1c6d7e8a90"),
        ("value", "12.5"),
        ("value", True),
        ("source", ""),
        ("dimensions", []),
    ],
)
def test_invalid_field_values_are_rejected(validator, field, bad):
    assert errors(validator, {**VALID_OK, field: bad})


@pytest.mark.parametrize("field", REQUIRED)
def test_every_field_is_required(validator, field):
    record = {k: v for k, v in VALID_OK.items() if k != field}
    assert errors(validator, record)


def test_unknown_extra_field_is_rejected(validator):
    assert errors(validator, {**VALID_OK, "note": "anything"})


def test_dimension_values_must_be_strings(validator):
    assert errors(validator, {**VALID_OK, "dimensions": {"aort_category": 3}})


def test_string_dimensions_are_accepted(validator):
    record = {**VALID_OK, "dimensions": {"aort_category": "savings"}}
    assert not errors(validator, record)
