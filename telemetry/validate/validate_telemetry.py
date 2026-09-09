"""Validation for AORT Phase 3 telemetry.

Proves, against a genuinely running stack, that each of the blueprint's four
candidate data categories is producing real, non-empty, CHANGING values - not
merely that the endpoints are reachable.

Method:

  1. Confirm every Prometheus scrape target is up.
  2. Capture a baseline sample of each category's metrics.
  3. Drive real banking activity with the Phase 2 workload generator, with
     OTLP export enabled so the generator's own telemetry flows too.
  4. Wait for the scrape and export intervals to elapse.
  5. Re-sample, and require that values are present, non-empty, and that
     enough of them actually moved.

Nothing here synthesises a metric value. If a source produces nothing, the
check fails and says so.

Usage (both stacks already running):

    python telemetry/validate/validate_telemetry.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
# Reuse the Phase 2 generator in place rather than forking a copy of it.
sys.path.insert(0, str(REPO_ROOT / "banking" / "workload"))

PROMETHEUS_URL = os.getenv("AORT_PROMETHEUS_URL", "http://localhost:9090")
OTLP_ENDPOINT = os.getenv("AORT_OTLP_ENDPOINT", "http://localhost:4318")

# Scrape jobs that must be up before any category can be judged.
EXPECTED_JOBS = {"prometheus", "fineract", "postgres", "cadvisor", "node", "workload-otel"}

# Candidate metrics per blueprint data category. Presence is verified live;
# whatever is actually found is what the README reports.
#   (label, promql, expect_change)
CATEGORIES: dict[str, list[tuple[str, str, bool]]] = {
    "1. Infrastructure": [
        ("container_cpu_usage_seconds_total",
         'sum(container_cpu_usage_seconds_total{name=~"aort-.+"})', True),
        ("container_memory_usage_bytes",
         'sum(container_memory_usage_bytes{name=~"aort-.+"})', False),
        ("container_network_receive_bytes_total",
         'sum(container_network_receive_bytes_total{name=~"aort-.+"})', True),
        # Machine-level, NOT per container: on Docker Desktop / WSL2 cAdvisor
        # emits filesystem series only with id="/" and no container name
        # label, so per-container disk usage is not attributable. Recorded as
        # a known gap in telemetry/README.md rather than worked around.
        ("container_fs_usage_bytes (machine-level)",
         'sum(container_fs_usage_bytes{id="/"})', False),
        ("node_filesystem_avail_bytes",
         'sum(node_filesystem_avail_bytes)', False),
        ("node_cpu_seconds_total", "sum(node_cpu_seconds_total)", True),
        ("node_memory_MemAvailable_bytes", "node_memory_MemAvailable_bytes", False),
        ("node_load1", "node_load1", False),
    ],
    "2. Application": [
        ("http_server_requests_seconds_count",
         "sum(http_server_requests_seconds_count)", True),
        ("http_server_requests_seconds_sum",
         "sum(http_server_requests_seconds_sum)", True),
        ("jvm_memory_used_bytes", "sum(jvm_memory_used_bytes)", False),
        ("jvm_threads_live_threads", "jvm_threads_live_threads", False),
        ("hikaricp_connections_active", "sum(hikaricp_connections_active)", False),
        ("process_cpu_usage", "process_cpu_usage", False),
    ],
    "3. Banking workload": [
        ("aort_workload_operations_total",
         "sum(aort_workload_operations_total)", True),
        ("aort_workload_operation_duration_seconds_count",
         "sum(aort_workload_operation_duration_seconds_count)", True),
        ("aort_workload_operation_duration_seconds_sum",
         "sum(aort_workload_operation_duration_seconds_sum)", True),
        ("aort_workload_http_responses_total",
         "sum(aort_workload_http_responses_total)", True),
    ],
    "4. Database": [
        ("pg_up", "pg_up", False),
        ("pg_stat_database_numbackends",
         'sum(pg_stat_database_numbackends{datname="fineract_default"})', False),
        ("pg_stat_database_xact_commit",
         'sum(pg_stat_database_xact_commit{datname="fineract_default"})', True),
        ("pg_stat_database_blks_hit",
         'sum(pg_stat_database_blks_hit{datname="fineract_default"})', True),
        ("pg_stat_database_tup_fetched",
         'sum(pg_stat_database_tup_fetched{datname="fineract_default"})', True),
        ("pg_database_size_bytes",
         'pg_database_size_bytes{datname="fineract_default"}', False),
    ],
}

# Minimum metrics per category that must have moved for the category to count
# as producing changing values.
MIN_CHANGED_PER_CATEGORY = 1


class Checks:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def add(self, name: str, passed: bool, detail: str = "") -> bool:
        self.results.append((name, passed, detail))
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
        return passed

    @property
    def failed(self) -> int:
        return sum(1 for _, ok, _ in self.results if not ok)


def promql(query: str) -> float | None:
    """Evaluate an instant query. Returns the first scalar value, or None."""
    try:
        resp = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=20
        )
        if not resp.ok:
            return None
        data = resp.json()
        if data.get("status") != "success":
            return None
        result = data["data"]["result"]
        if not result:
            return None
        return float(result[0]["value"][1])
    except (requests.RequestException, ValueError, KeyError, IndexError):
        return None


def scrape_targets() -> dict[str, str]:
    """Map job name -> health, from Prometheus's own target list."""
    try:
        resp = requests.get(f"{PROMETHEUS_URL}/api/v1/targets", timeout=20)
        resp.raise_for_status()
        return {
            t["labels"]["job"]: t["health"]
            for t in resp.json()["data"]["activeTargets"]
        }
    except (requests.RequestException, ValueError, KeyError):
        return {}


def wait_for_targets(timeout_s: float) -> dict[str, str]:
    """Poll until every expected job reports up, or the timeout expires."""
    deadline = time.monotonic() + timeout_s
    health: dict[str, str] = {}
    while time.monotonic() < deadline:
        health = scrape_targets()
        if health and all(health.get(j) == "up" for j in EXPECTED_JOBS):
            return health
        time.sleep(5)
    return health


def sample_all() -> dict[str, dict[str, float | None]]:
    return {
        category: {label: promql(q) for label, q, _ in metrics}
        for category, metrics in CATEGORIES.items()
    }


def run_workload(clients: int, iterations: int) -> tuple[int, int]:
    """Drive real banking activity through the Phase 2 generator."""
    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = OTLP_ENDPOINT

    import aort_telemetry
    from aort_workload.client import FineractClient
    from aort_workload.config import load_config
    from aort_workload.report import RunReport
    from aort_workload.runner import WorkloadOptions, run_workload as _run

    if not aort_telemetry.is_enabled():
        raise RuntimeError(
            "aort_telemetry did not activate; OTLP export would not be exercised"
        )

    report = RunReport(f"telemetry-validate-{datetime.now():%Y%m%dT%H%M%S}", None)
    client = FineractClient(load_config(), report)
    client.wait_until_ready(timeout_seconds=900)
    _run(client, WorkloadOptions(new_clients=clients, iterations=iterations,
                                 loans_per_run=2, seed=20260909), date.today())
    aort_telemetry.shutdown()
    return report.succeeded, report.total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate AORT Phase 3 telemetry.")
    parser.add_argument("--clients", type=int, default=4)
    parser.add_argument("--iterations", type=int, default=60)
    parser.add_argument("--settle-seconds", type=float, default=30.0,
                        help="wait after the workload for scrape/export intervals")
    parser.add_argument("--target-timeout", type=float, default=180.0)
    parser.add_argument("--output", type=Path,
                        default=REPO_ROOT / "telemetry" / "validate" / "runs")
    args = parser.parse_args(argv)

    checks = Checks()
    print("AORT Phase 3 - telemetry validation")
    print(f"  prometheus : {PROMETHEUS_URL}")
    print(f"  otlp       : {OTLP_ENDPOINT}\n")

    # --- 1. targets -----------------------------------------------------
    print("Scrape targets")
    health = wait_for_targets(args.target_timeout)
    if not health:
        checks.add("Prometheus reachable", False, f"no response from {PROMETHEUS_URL}")
        print("\nIs the stack up?  cd telemetry && docker compose up -d")
        return 1
    checks.add("Prometheus reachable", True, f"{len(health)} active targets")
    for job in sorted(EXPECTED_JOBS):
        checks.add(f"Target up: {job}", health.get(job) == "up",
                   f"health={health.get(job, 'MISSING')}")

    # --- 2. baseline ----------------------------------------------------
    print("\nBaseline sample")
    before = sample_all()
    for category, values in before.items():
        found = sum(1 for v in values.values() if v is not None)
        print(f"  {category}: {found}/{len(values)} metrics present")

    # --- 3. real workload ----------------------------------------------
    print("\nGenerating real banking activity (Phase 2 workload generator)")
    try:
        ok_calls, total_calls = run_workload(args.clients, args.iterations)
        checks.add("Workload generated real activity", total_calls > 0 and ok_calls == total_calls,
                   f"{ok_calls}/{total_calls} Fineract calls succeeded")
    except Exception as exc:  # noqa: BLE001
        checks.add("Workload generated real activity", False, f"{type(exc).__name__}: {exc}")
        return 1

    print(f"\nWaiting {args.settle_seconds:.0f}s for scrape and export intervals")
    time.sleep(args.settle_seconds)

    # --- 4. re-sample and judge ----------------------------------------
    after = sample_all()
    print("\nPer-category results")
    evidence: dict[str, Any] = {}

    for category, metrics in CATEGORIES.items():
        print(f"\n{category}")
        rows = []
        present = 0
        changed = 0
        for label, query, expect_change in metrics:
            b = before[category][label]
            a = after[category][label]
            is_present = a is not None
            # A series that did not exist at baseline and carries a value now
            # appeared during the run. On a genuinely clean slate that is the
            # strongest evidence of real data, not the weakest - the workload
            # metrics cannot exist before the workload has run.
            appeared = is_present and b is None
            did_change = is_present and (b is None or a != b)
            present += int(is_present)
            changed += int(did_change)
            marker = "present" if is_present else "MISSING"
            delta = f"{b} -> {a}" if is_present else "no data"
            if appeared:
                flag = "  (appeared)"
            elif did_change:
                flag = "  (changed)"
            else:
                flag = ""
            print(f"    {label:<48} {marker:<8} {delta}{flag}")
            rows.append({
                "metric": label, "query": query, "present": is_present,
                "before": b, "after": a, "changed": did_change,
                "appeared_during_run": appeared,
                "change_expected": expect_change,
            })

        checks.add(f"{category}: metrics present",
                   present == len(metrics), f"{present}/{len(metrics)}")
        checks.add(f"{category}: values changing",
                   changed >= MIN_CHANGED_PER_CATEGORY,
                   f"{changed} metric(s) appeared or moved during the run")
        evidence[category] = rows

    # --- output ---------------------------------------------------------
    args.output.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    path = args.output / f"telemetry-validation-{stamp}.json"
    path.write_text(json.dumps({
        "captured_at": datetime.now().astimezone().isoformat(),
        "prometheus_url": PROMETHEUS_URL,
        "targets": health,
        "categories": evidence,
    }, indent=2), encoding="utf-8")

    total = len(checks.results)
    print("\n" + "=" * 72)
    if checks.failed:
        print(f"TELEMETRY VALIDATION FAILED - {checks.failed} of {total} checks failed")
    else:
        print(f"TELEMETRY VALIDATION PASSED - all {total} checks passed")
    print("=" * 72)
    print(f"Evidence: {path}")
    return 1 if checks.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
