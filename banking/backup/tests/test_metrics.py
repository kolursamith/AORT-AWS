"""Parsing the backup-state metrics the sidecar writes for node-exporter."""

from __future__ import annotations

import math

import pytest

from aort_backup.metrics import backup_age_seconds, parse_textfile

SAMPLE = """\
# HELP aort_backup_runs_total Backup attempts since the sidecar started.
# TYPE aort_backup_runs_total counter
aort_backup_runs_total 3
# HELP aort_backup_failures_total Failed backup attempts.
# TYPE aort_backup_failures_total counter
aort_backup_failures_total 1
# HELP aort_backup_last_success_timestamp_seconds Unix time of the last successful backup.
# TYPE aort_backup_last_success_timestamp_seconds gauge
aort_backup_last_success_timestamp_seconds 1789491395
# TYPE aort_backup_last_duration_seconds gauge
aort_backup_last_duration_seconds 0.42
# TYPE aort_backup_last_size_bytes gauge
aort_backup_last_size_bytes 1048576
"""


def test_parses_every_metric():
    assert parse_textfile(SAMPLE) == {
        "aort_backup_runs_total": 3.0,
        "aort_backup_failures_total": 1.0,
        "aort_backup_last_success_timestamp_seconds": 1789491395.0,
        "aort_backup_last_duration_seconds": 0.42,
        "aort_backup_last_size_bytes": 1048576.0,
    }


@pytest.mark.parametrize("text", ["", "\n\n", "# HELP only_comment x\n# TYPE only_comment gauge\n"])
def test_file_without_samples_parses_to_empty(text):
    assert parse_textfile(text) == {}


def test_blank_lines_and_trailing_whitespace_are_tolerated():
    assert parse_textfile("\n  aort_backup_runs_total   7  \n\n") == {"aort_backup_runs_total": 7.0}


def test_nan_value_is_preserved_as_nan():
    parsed = parse_textfile("aort_backup_last_size_bytes NaN\n")
    assert math.isnan(parsed["aort_backup_last_size_bytes"])


@pytest.mark.parametrize("line", ["aort_backup_runs_total", "aort_backup_runs_total abc",
                                  "aort_backup_runs_total 1 2 3"])
def test_malformed_sample_line_raises(line):
    with pytest.raises(ValueError):
        parse_textfile(line + "\n")


def test_labelled_series_keeps_its_labels_in_the_key():
    parsed = parse_textfile('aort_backup_last_size_bytes{database="fineract_default"} 5\n')
    assert parsed == {'aort_backup_last_size_bytes{database="fineract_default"}': 5.0}


# --- age (the RPO input) -------------------------------------------------------

def test_age_is_now_minus_last_success():
    metrics = {"aort_backup_last_success_timestamp_seconds": 1000.0}
    assert backup_age_seconds(metrics, now=1075.5) == 75.5


def test_age_without_a_successful_backup_is_none_not_zero():
    # No successful backup yet must not look like a fresh backup.
    assert backup_age_seconds({"aort_backup_runs_total": 2.0}, now=1000.0) is None


def test_age_is_clamped_to_zero_when_clocks_disagree_slightly():
    metrics = {"aort_backup_last_success_timestamp_seconds": 1000.5}
    assert backup_age_seconds(metrics, now=1000.0) == 0.0
