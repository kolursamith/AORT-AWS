"""Against the real running stacks: real banking activity in, valid I1 out.

Run with:  pytest --live
With --live these tests FAIL (not skip) if the stack is down, so a green run is
evidence the stack was genuinely exercised.
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from datetime import date
from pathlib import Path

import pytest
import requests

pytestmark = pytest.mark.live

PROM = os.getenv("AORT_PROMETHEUS_URL", "http://localhost:9090")
REPO = Path(__file__).resolve().parents[3]
BANKING_CATEGORIES = {"client", "savings", "loan", "transaction", "accounting"}


@pytest.fixture(scope="module")
def live_snapshot():
    ready = requests.get(f"{PROM}/-/ready", timeout=10)
    assert ready.status_code == 200, "Prometheus is not ready"

    # Real banking activity through the Phase 2 generator, OTLP enabled.
    os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    sys.path.insert(0, str(REPO / "banking" / "workload"))
    import aort_telemetry
    from aort_workload.client import FineractClient
    from aort_workload.config import load_config
    from aort_workload.report import RunReport
    from aort_workload.runner import WorkloadOptions, run_workload

    # Unique per run: the generator derives client externalIds from the run id
    # and the seed, and Fineract rejects duplicates. A fixed id made every
    # second live run collide with the first (observed 2026-09-15).
    report = RunReport(f"normalizer-live-{uuid.uuid4().hex[:8]}", None)
    client = FineractClient(load_config(), report)
    client.wait_until_ready(timeout_seconds=600)
    # Steady traffic, not a burst. A counter series whose whole increase lands
    # before its first scrape looks flat to rate(), so a back-to-back burst of
    # ~50 calls in ~8 s reads as "0 operations/s" (observed 2026-09-15). Spacing
    # the activity loop across several 10 s scrapes mirrors the continuously
    # running workload the twin is meant to observe.
    run_workload(
        client,
        WorkloadOptions(new_clients=2, iterations=30, loans_per_run=1, seed=1509,
                        delay_seconds=1.0),
        date.today(),
    )
    aort_telemetry.shutdown()
    assert report.total > 0 and report.failed == 0, "workload did not run cleanly"

    time.sleep(15)  # one more scrape plus the OTLP export interval

    from aort_normalizer.normalize import collect_snapshot
    from aort_normalizer.prometheus import PrometheusClient

    return collect_snapshot(PrometheusClient(PROM))


def ungrouped(snapshot):
    return {(o.component_id, o.signal): o for o in snapshot if not o.dimensions}


def direct(promql):
    resp = requests.get(f"{PROM}/api/v1/query", params={"query": promql}, timeout=10).json()
    return float(resp["data"]["result"][0]["value"][1])


def test_live_snapshot_satisfies_the_contract(live_snapshot, validator):
    for obs in live_snapshot:
        assert not list(validator.iter_errors(obs.to_dict())), obs


def test_live_catalog_queries_are_never_ambiguous(live_snapshot):
    assert [o for o in live_snapshot if o.quality == "ambiguous"] == []


def test_live_core_banking_components_are_up(live_snapshot):
    obs = ungrouped(live_snapshot)
    assert obs[("fineract", "up")].value == 1.0
    assert obs[("postgres", "up")].value == 1.0


def test_live_every_signal_is_present_and_plausible_after_workload(live_snapshot):
    bad = [(o.component_id, o.signal, o.dimensions, o.quality) for o in live_snapshot
           if o.quality != "ok"]
    assert bad == []


def test_live_banking_operations_are_broken_down_by_category(live_snapshot):
    categories = {o.dimensions.get("aort_category") for o in live_snapshot
                  if o.component_id == "banking-operations" and o.dimensions}
    assert len(categories & BANKING_CATEGORIES) >= 4, categories


def test_live_values_agree_with_an_independent_direct_query(live_snapshot):
    obs = ungrouped(live_snapshot)
    assert obs[("postgres", "up")].value == direct("pg_up")
    assert obs[("fineract", "up")].value == direct('up{job="fineract"}')
    size = direct('sum(pg_database_size_bytes{datname="fineract_default"})')
    assert obs[("postgres", "database_size_bytes")].value == pytest.approx(size, rel=0.01)
