"""Turning Prometheus samples into I1 observations, without inventing data."""

from __future__ import annotations

import json
import uuid

import pytest

from aort_normalizer import normalize as normalize_module
from aort_normalizer.catalog import CATALOG, SignalSpec
from aort_normalizer.normalize import (
    SCHEMA_VERSION,
    collect_snapshot,
    format_timestamp,
    normalize_samples,
)
from aort_normalizer.prometheus import Sample, TelemetrySourceError

SNAP = "0b0d6a5e-3f2a-4c55-9a7e-2f1c6d7e8a90"
OBSERVED = "2001-09-09T01:46:40.500Z"
AT = 1_000_000_000.5


def spec(**overrides):
    base = dict(
        component_id="postgres",
        signal="commit_rate",
        category="database",
        unit="transactions_per_second",
        promql="sum(rate(pg_stat_database_xact_commit[1m]))",
        source="prometheus:job=postgres",
        group_by=(),
        bounds=(0.0, None),
    )
    base.update(overrides)
    return SignalSpec(**base)


RATIO = spec(signal="cache_hit_ratio", unit="ratio", bounds=(0.0, 1.0))
GROUPED = spec(
    component_id="banking-operations",
    signal="operation_rate",
    category="banking_workload",
    unit="operations_per_second",
    promql="sum by (aort_category) (rate(aort_workload_operations_total[1m]))",
    source="prometheus:job=workload-otel",
    group_by=("aort_category",),
)


def run(s, samples):
    return normalize_samples(s, samples, snapshot_id=SNAP, observed_at=OBSERVED)


# --- timestamps --------------------------------------------------------------

def test_timestamps_are_utc_with_millisecond_precision():
    assert format_timestamp(AT) == OBSERVED
    assert format_timestamp(0) == "1970-01-01T00:00:00.000Z"


# --- single-series signals ---------------------------------------------------

def test_single_finite_sample_is_ok():
    [obs] = run(spec(), [Sample({}, "12.5")])
    assert (obs.value, obs.quality, obs.dimensions) == (12.5, "ok", {})


def test_no_samples_is_missing_and_never_zero():
    [obs] = run(spec(), [])
    assert (obs.value, obs.quality) == (None, "missing")


@pytest.mark.parametrize("raw", ["NaN", "+Inf", "-Inf", "abc", ""])
def test_non_finite_or_unparseable_values_carry_no_value(raw):
    [obs] = run(spec(), [Sample({}, raw)])
    assert (obs.value, obs.quality) == (None, "non_finite")


def test_several_series_for_an_ungrouped_signal_is_ambiguous():
    [obs] = run(spec(), [Sample({"a": "1"}, "1"), Sample({"a": "2"}, "2")])
    assert (obs.value, obs.quality) == (None, "ambiguous")


# --- bounds ------------------------------------------------------------------

def test_value_above_upper_bound_is_flagged_but_kept():
    [obs] = run(RATIO, [Sample({}, "1.5")])
    assert (obs.value, obs.quality) == (1.5, "out_of_bounds")


def test_negative_value_below_lower_bound_is_flagged_but_kept():
    [obs] = run(spec(), [Sample({}, "-0.1")])
    assert (obs.value, obs.quality) == (-0.1, "out_of_bounds")


@pytest.mark.parametrize("raw,expected", [("0", 0.0), ("1", 1.0)])
def test_values_exactly_on_a_bound_are_ok(raw, expected):
    [obs] = run(RATIO, [Sample({}, raw)])
    assert (obs.value, obs.quality) == (expected, "ok")


# --- grouped signals ---------------------------------------------------------

def test_grouped_signal_emits_one_record_per_group():
    observations = run(
        GROUPED,
        [
            Sample({"aort_category": "savings"}, "2"),
            Sample({"aort_category": "loan"}, "1"),
        ],
    )
    got = {o.dimensions["aort_category"]: (o.value, o.quality) for o in observations}
    assert got == {"savings": (2.0, "ok"), "loan": (1.0, "ok")}


def test_grouped_signal_missing_label_is_reported_as_unknown():
    [obs] = run(GROUPED, [Sample({}, "3")])
    assert obs.dimensions == {"aort_category": "unknown"}


def test_grouped_signal_without_samples_is_one_missing_record():
    [obs] = run(GROUPED, [])
    assert (obs.value, obs.quality, obs.dimensions) == (None, "missing", {})


def test_duplicate_groups_are_ambiguous():
    observations = run(
        GROUPED,
        [
            Sample({"aort_category": "loan"}, "1"),
            Sample({"aort_category": "loan"}, "2"),
            Sample({"aort_category": "savings"}, "4"),
        ],
    )
    got = {o.dimensions["aort_category"]: (o.value, o.quality) for o in observations}
    assert got == {"loan": (None, "ambiguous"), "savings": (4.0, "ok")}


# --- record content ----------------------------------------------------------

def test_record_carries_the_signal_metadata():
    [obs] = run(spec(), [Sample({}, "1")])
    d = obs.to_dict()
    assert d["schema_version"] == SCHEMA_VERSION == "i1-provisional-0.1"
    assert d["snapshot_id"] == SNAP
    assert d["observed_at"] == OBSERVED
    assert (d["component_id"], d["signal"], d["category"], d["unit"], d["source"]) == (
        "postgres",
        "commit_rate",
        "database",
        "transactions_per_second",
        "prometheus:job=postgres",
    )


CASES = [
    (spec(), [Sample({}, "12.5")]),
    (spec(), []),
    (spec(), [Sample({}, "NaN")]),
    (spec(), [Sample({"a": "1"}, "1"), Sample({"a": "2"}, "2")]),
    (RATIO, [Sample({}, "1.5")]),
    (GROUPED, [Sample({"aort_category": "savings"}, "2"), Sample({}, "1")]),
    (GROUPED, []),
]


@pytest.mark.parametrize("signal_spec,samples", CASES)
def test_every_record_satisfies_the_contract(validator, signal_spec, samples):
    for obs in run(signal_spec, samples):
        record = obs.to_dict()
        assert not list(validator.iter_errors(record)), record
        json.dumps(record, allow_nan=False)  # never NaN/Inf on the wire


# --- snapshots ---------------------------------------------------------------

def one_sample_per_spec(promql):
    for s in CATALOG:
        if s.promql == promql and s.group_by:
            return [
                Sample({s.group_by[0]: "savings"}, "0.5"),
                Sample({s.group_by[0]: "loan"}, "0.25"),
            ]
    return [Sample({}, "0.5")]


def test_snapshot_queries_every_catalog_entry_once_at_one_instant(fake_client_cls):
    client = fake_client_cls(default=one_sample_per_spec)
    collect_snapshot(client, at=AT, snapshot_id=SNAP)
    assert len(client.calls) == len(CATALOG)
    assert {q for q, _ in client.calls} == {s.promql for s in CATALOG}
    assert {t for _, t in client.calls} == {AT}


def test_snapshot_records_share_one_id_and_timestamp(fake_client_cls):
    snapshot = collect_snapshot(fake_client_cls(default=one_sample_per_spec), at=AT)
    ids = {o.snapshot_id for o in snapshot}
    assert len(ids) == 1
    assert uuid.UUID(ids.pop()).version == 4
    assert {o.observed_at for o in snapshot} == {OBSERVED}


def test_snapshot_defaults_to_the_current_time(fake_client_cls, monkeypatch):
    monkeypatch.setattr(normalize_module.time, "time", lambda: AT)
    snapshot = collect_snapshot(fake_client_cls(default=one_sample_per_spec))
    assert {o.observed_at for o in snapshot} == {OBSERVED}


def test_full_snapshot_satisfies_the_contract_with_unique_keys(fake_client_cls, validator):
    snapshot = collect_snapshot(fake_client_cls(default=one_sample_per_spec), at=AT)
    keys = []
    for obs in snapshot:
        assert not list(validator.iter_errors(obs.to_dict()))
        keys.append((obs.component_id, obs.signal, tuple(sorted(obs.dimensions.items()))))
    assert len(keys) == len(set(keys))


def test_snapshot_never_invents_values_when_everything_is_missing(fake_client_cls):
    snapshot = collect_snapshot(fake_client_cls(), at=AT)
    assert len(snapshot) == len(CATALOG)
    assert all(o.value is None and o.quality == "missing" for o in snapshot)


def test_snapshot_propagates_source_errors(fake_client_cls):
    client = fake_client_cls(error=TelemetrySourceError("prometheus unreachable"))
    with pytest.raises(TelemetrySourceError, match="unreachable"):
        collect_snapshot(client, at=AT)
