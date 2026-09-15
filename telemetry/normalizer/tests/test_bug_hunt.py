"""Bug-hunt pass: hostile inputs and failure paths beyond the happy path.

Each test pins down a behaviour that would otherwise crash, corrupt the
contract, or silently mislead the digital twin.
"""

from __future__ import annotations

import json

import pytest

from aort_normalizer.__main__ import main
from aort_normalizer.catalog import CATALOG, SignalSpec
from aort_normalizer.normalize import collect_snapshot, normalize_samples
from aort_normalizer.prometheus import PrometheusClient, Sample, TelemetrySourceError

from test_prometheus_client import FakeResponse, FakeSession

SNAP = "0b0d6a5e-3f2a-4c55-9a7e-2f1c6d7e8a90"
OBSERVED = "2001-09-09T01:46:40.500Z"


def client_returning(payload, status=200):
    return PrometheusClient("http://p:9090", session=FakeSession(FakeResponse(status, payload)))


RATE = SignalSpec("postgres", "commit_rate", "database", "transactions_per_second",
                  "sum(rate(x[1m]))", "prometheus:job=postgres")
GROUPED = SignalSpec("banking-operations", "operation_rate", "banking_workload",
                     "operations_per_second", "sum by (aort_category) (rate(y[1m]))",
                     "prometheus:job=workload-otel", group_by=("aort_category",))


# --- Prometheus response shapes ------------------------------------------------

@pytest.mark.parametrize("data", [[], "oops", 7])
def test_data_field_that_is_not_an_object_is_a_source_error(data):
    client = client_returning({"status": "success", "data": data})
    with pytest.raises(TelemetrySourceError):
        client.query("up", at=0)


def test_result_that_is_not_a_list_is_a_source_error():
    body = {"status": "success", "data": {"resultType": "vector", "result": {"metric": {}}}}
    with pytest.raises(TelemetrySourceError):
        client_returning(body).query("up", at=0)


@pytest.mark.parametrize("value", [[1.0], [1.0, "1", "extra"], "1", None])
def test_series_value_with_the_wrong_shape_is_a_source_error(value):
    body = {"status": "success",
            "data": {"resultType": "vector", "result": [{"metric": {}, "value": value}]}}
    with pytest.raises(TelemetrySourceError):
        client_returning(body).query("up", at=0)


def test_json_body_that_is_a_list_is_a_source_error():
    with pytest.raises(TelemetrySourceError):
        client_returning(["not", "an", "object"]).query("up", at=0)


def test_metric_name_label_is_stripped():
    body = {"status": "success", "data": {"resultType": "vector", "result": [
        {"metric": {"__name__": "up", "job": "fineract"}, "value": [0, "1"]}]}}
    assert client_returning(body).query("up", at=0) == [Sample({"job": "fineract"}, "1")]


# --- value parsing ---------------------------------------------------------------

def test_negative_zero_is_normalized_to_zero():
    [obs] = normalize_samples(RATE, [Sample({}, "-0")], SNAP, OBSERVED)
    assert obs.quality == "ok"
    assert json.dumps(obs.value) == "0.0"


@pytest.mark.parametrize("raw,expected", [("1.5e-05", 1.5e-05), (" 3 ", 3.0), ("1e308", 1e308)])
def test_valid_numeric_forms_parse(raw, expected):
    [obs] = normalize_samples(RATE, [Sample({}, raw)], SNAP, OBSERVED)
    assert (obs.value, obs.quality) == (expected, "ok")


def test_overflowing_value_is_non_finite_not_infinite():
    [obs] = normalize_samples(RATE, [Sample({}, "1e309")], SNAP, OBSERVED)
    assert (obs.value, obs.quality) == (None, "non_finite")


def test_several_unlabelled_samples_in_a_grouped_signal_are_ambiguous():
    [obs] = normalize_samples(GROUPED, [Sample({}, "1"), Sample({}, "2")], SNAP, OBSERVED)
    assert (obs.value, obs.quality, obs.dimensions) == (None, "ambiguous",
                                                        {"aort_category": "unknown"})


# --- snapshot integrity ------------------------------------------------------------

def test_epoch_zero_is_respected_not_replaced_by_now(fake_client_cls):
    snapshot = collect_snapshot(fake_client_cls(), at=0, snapshot_id=SNAP)
    assert {o.observed_at for o in snapshot} == {"1970-01-01T00:00:00.000Z"}


@pytest.mark.parametrize("bad", ["not-a-uuid", "0b0d6a5e-3f2a-1c55-9a7e-2f1c6d7e8a90", ""])
def test_invalid_caller_supplied_snapshot_id_is_rejected(fake_client_cls, bad):
    with pytest.raises(ValueError):
        collect_snapshot(fake_client_cls(), at=0, snapshot_id=bad)


def test_empty_catalog_yields_an_empty_snapshot(fake_client_cls):
    assert collect_snapshot(fake_client_cls(), catalog=(), at=0) == []


# --- CLI failure paths ---------------------------------------------------------------

def one_sample(_promql):
    return [Sample({}, "1")]


def make_factory(client):
    return lambda url, timeout: client


def test_cli_rejects_a_zero_timeout(fake_client_cls):
    with pytest.raises(SystemExit) as exc:
        main(["--timeout", "0"], client_factory=make_factory(fake_client_cls(default=one_sample)))
    assert exc.value.code == 2


def test_cli_unwritable_output_exits_3_with_a_message(tmp_path, fake_client_cls, capsys):
    # A directory cannot be opened for appending.
    rc = main(["--out", str(tmp_path)],
              client_factory=make_factory(fake_client_cls(default=one_sample)))
    assert rc == 3
    assert "cannot write" in capsys.readouterr().err


class FailsOnSecondSnapshot:
    def __init__(self):
        self.calls = 0

    def query(self, promql, at):
        self.calls += 1
        if self.calls > len(CATALOG):
            raise TelemetrySourceError("prometheus went away")
        return [Sample({}, "1")]


def test_cli_keeps_completed_snapshots_when_a_later_one_fails(tmp_path):
    out = tmp_path / "s.jsonl"
    rc = main(["--out", str(out), "--count", "2", "--interval", "0"],
              client_factory=make_factory(FailsOnSecondSnapshot()))
    assert rc == 2
    assert len(out.read_text(encoding="utf-8").splitlines()) == len(CATALOG)
