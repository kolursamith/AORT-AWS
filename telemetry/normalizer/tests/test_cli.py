"""Command-line entry point: JSON Lines out, clear failure when the source is down."""

from __future__ import annotations

import json

import pytest

from aort_normalizer.__main__ import main
from aort_normalizer.catalog import CATALOG
from aort_normalizer.prometheus import Sample, TelemetrySourceError


def factory(client, seen=None):
    def make(url, timeout):
        if seen is not None:
            seen.append((url, timeout))
        return client

    return make


def one_sample(_promql):
    return [Sample({}, "1")]


def test_writes_one_valid_jsonl_record_per_observation(tmp_path, fake_client_cls, validator):
    out = tmp_path / "snap.jsonl"
    rc = main(
        ["--prometheus-url", "http://p:9090", "--out", str(out)],
        client_factory=factory(fake_client_cls(default=one_sample)),
    )
    assert rc == 0
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(CATALOG)
    for line in lines:
        assert not list(validator.iter_errors(json.loads(line)))


def test_passes_the_url_to_the_client(tmp_path, fake_client_cls):
    seen = []
    main(
        ["--prometheus-url", "http://p:9090", "--out", str(tmp_path / "s.jsonl")],
        client_factory=factory(fake_client_cls(default=one_sample), seen),
    )
    assert seen[0][0] == "http://p:9090"


def test_url_defaults_to_the_environment(tmp_path, fake_client_cls, monkeypatch):
    monkeypatch.setenv("AORT_PROMETHEUS_URL", "http://from-env:9090")
    seen = []
    main(["--out", str(tmp_path / "s.jsonl")],
         client_factory=factory(fake_client_cls(default=one_sample), seen))
    assert seen[0][0] == "http://from-env:9090"


def test_source_error_exits_2_and_writes_nothing(tmp_path, fake_client_cls, capsys):
    out = tmp_path / "snap.jsonl"
    client = fake_client_cls(error=TelemetrySourceError("prometheus unreachable"))
    rc = main(["--out", str(out)], client_factory=factory(client))
    assert rc == 2
    assert not out.exists()
    assert "prometheus unreachable" in capsys.readouterr().err


def test_count_produces_distinct_snapshots(tmp_path, fake_client_cls):
    out = tmp_path / "snap.jsonl"
    rc = main(["--out", str(out), "--count", "2", "--interval", "0"],
              client_factory=factory(fake_client_cls(default=one_sample)))
    assert rc == 0
    records = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 2 * len(CATALOG)
    assert len({r["snapshot_id"] for r in records}) == 2


def test_appends_so_repeated_runs_build_a_series(tmp_path, fake_client_cls):
    out = tmp_path / "snap.jsonl"
    for _ in range(2):
        main(["--out", str(out)], client_factory=factory(fake_client_cls(default=one_sample)))
    assert len(out.read_text(encoding="utf-8").splitlines()) == 2 * len(CATALOG)


def test_writes_to_stdout_without_out(fake_client_cls, capsys):
    rc = main([], client_factory=factory(fake_client_cls(default=one_sample)))
    assert rc == 0
    assert len(capsys.readouterr().out.splitlines()) == len(CATALOG)


@pytest.mark.parametrize("args", [["--count", "0"], ["--interval", "-1"]])
def test_rejects_invalid_arguments(args, fake_client_cls):
    with pytest.raises(SystemExit) as exc:
        main(args, client_factory=factory(fake_client_cls(default=one_sample)))
    assert exc.value.code == 2
