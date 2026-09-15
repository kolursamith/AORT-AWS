"""Guards on the telemetry pipeline that the normalizer's rate() signals rely on.

Found by the Layer 1a live test on 2026-09-15: with the collector's Prometheus
exporter set to ``send_timestamps: true``, a workload counter keeps its last
OTel export timestamp after the generator stops. Prometheus deduplicates every
later scrape of that frozen timestamp, so each series holds a single sample and
``rate()`` over it is impossible. Measured live: 32 of 32
``aort_workload_operations_total`` series had exactly one sample in 10 minutes.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
COLLECTOR_CONFIG = REPO_ROOT / "telemetry" / "otel-collector" / "config.yml"
PROMETHEUS_CONFIG = REPO_ROOT / "telemetry" / "prometheus" / "prometheus.yml"

# Longest rate() window any catalog query uses (postgres cache_hit_ratio).
LONGEST_RATE_WINDOW_S = 5 * 60


def _seconds(duration: str) -> int:
    match = re.fullmatch(r"(\d+)([smh])", str(duration).strip())
    assert match, f"unrecognised duration {duration!r}"
    return int(match.group(1)) * {"s": 1, "m": 60, "h": 3600}[match.group(2)]


def _exporter():
    config = yaml.safe_load(COLLECTOR_CONFIG.read_text(encoding="utf-8"))
    return config["exporters"]["prometheus"]


def test_prometheus_assigns_scrape_timestamps_to_workload_samples():
    assert _exporter().get("send_timestamps", False) is False


def test_workload_series_outlive_the_longest_rate_window():
    assert _seconds(_exporter()["metric_expiration"]) > LONGEST_RATE_WINDOW_S


def test_scrape_interval_leaves_at_least_two_samples_per_one_minute_window():
    config = yaml.safe_load(PROMETHEUS_CONFIG.read_text(encoding="utf-8"))
    assert _seconds(config["global"]["scrape_interval"]) <= 30
