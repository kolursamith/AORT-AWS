"""Stage 09 controller benchmark: measured latencies on this machine, not a guarantee.

For each pending-pool size the benchmark builds a ``ControllerCore`` (ManualClock), feeds 420 s
of synthetic own-traffic telemetry (the open-route profile: 30, 45, then 20 rps) and ``n``
ambiguous transactions, then issues ``repeats`` decision requests. Reported per size: request
validation, twin snapshot + estimator, pool construction, look-ahead + optimizer and total
decision latency (median, p95, max in ms), response size, telemetry and event intake latency,
and the fallback/error rate. Budgets are set from these measurements and checked by the
benchmark test; they are not claimed for other hardware.
"""

import json
import platform
import statistics
import sys
import time
from datetime import timedelta
from pathlib import Path

from contracts.v1_1 import (
    SCHEMA_VERSION,
    DecisionRequest,
    RouteDescriptor,
    RouteTelemetry,
    TransactionEvent,
    ValidatedRouteTelemetry,
)
from contracts.v1_1.protocol import encode
from intelligence.common.clock import ManualClock
from intelligence.controller.service import ControllerConfig, ControllerCore, RouteRegistration
from intelligence.simulation.scenarios import research_timing_policy
from intelligence.twin.route_twin import RouteTwinConfig

EXP, ROUTE = "EXP-BENCH", "A1-B"
POOL_SIZES = (100, 1000, 5000)
REPEATS = 20
# Budgets declared before the first run (p95 total decision latency); the first run on an
# Apple Silicon laptop (Python 3.11) measured p95 8.6, 54 and 277 ms against them.
BUDGETS_MS = {100: 50.0, 1000: 250.0, 5000: 1500.0}


def _core(clock):
    policy = research_timing_policy()
    config = ControllerConfig(
        experiment_id=EXP,
        timing_policy=policy,
        routes=(
            RouteRegistration(
                descriptor=RouteDescriptor(
                    schema_version=SCHEMA_VERSION,
                    experiment_id=EXP,
                    route_id=ROUTE,
                    remitter_bank_id="A1",
                    switch_id="SW",
                    beneficiary_bank_id="B",
                ),
                telemetry_sources=("src-A1-B",),
            ),
        ),
        event_sources=("remitter-A1",),
        twin=RouteTwinConfig(
            model_version="rt-1",
            max_observation_age_s=30.0,
            max_future_skew_s=1.0,
            freshness_threshold_s=5.0,
        ),
    )
    return ControllerCore(config, clock), policy


def _window(t0, k, rate):
    start = t0 + timedelta(seconds=k)
    n = int(rate)
    obs = RouteTelemetry(
        schema_version=SCHEMA_VERSION,
        experiment_id=EXP,
        route_id=ROUTE,
        source_id="src-A1-B",
        observation_scope="REMITTER_OWN_TRAFFIC",
        sequence=k,
        observation_window_start=start,
        observation_window_end=start + timedelta(seconds=1),
        observation_timestamp=start + timedelta(seconds=1),
        payment_rps=rate,
        check_rps=0.0,
        observed_throughput_rps=rate,
        p50_latency_ms=150.0,
        p95_latency_ms=300.0,
        latency_sample_count=n,
        timeout_rate=0.0,
        timeout_count=0,
        timeout_denominator_count=n,
        queue_depth=3,
    )
    return ValidatedRouteTelemetry(
        schema_version=SCHEMA_VERSION,
        experiment_id=EXP,
        route_id=ROUTE,
        telemetry=obs,
        trust_status="AUTHENTICATED",
        validator_id="bench",
        validator_version="bench-1",
        validated_at=obs.observation_timestamp,
        cross_check_status="UNAVAILABLE",
    )


def _event(t0, txn, kind, version, t, deadline=None):
    extra = {}
    if deadline is not None:
        extra = {
            "deadline_at": t0 + timedelta(seconds=deadline),
            "deadline_policy_id": "report-table12-research",
        }
    return TransactionEvent(
        schema_version=SCHEMA_VERSION,
        experiment_id=EXP,
        event_id=f"{txn}:v{version}",
        transaction_id=txn,
        route_id=ROUTE,
        remitter_bank_id="A1",
        beneficiary_bank_id="B",
        event_type=kind,
        ledger_state="DEBITED",
        financial_outcome="UNKNOWN",
        state_version=version,
        amount_minor=50000,
        event_timestamp=t0 + timedelta(seconds=t),
        source_id="remitter-A1",
        source_sequence=version,
        correlation_id=f"PAY-{txn}",
        counted_check_attempts=0,
        **extra,
    )


def _pct(values, q):
    ordered = sorted(values)
    return ordered[max(0, int(round(q * len(ordered))) - 1)]


def run_size(n: int, repeats: int = REPEATS) -> dict:
    from datetime import UTC, datetime

    t0 = datetime(2026, 6, 1, 8, 0, tzinfo=UTC)
    clock = ManualClock(t0)
    core, _ = _core(clock)
    tel_ms, ev_ms = [], []
    events = []
    for i in range(n):
        t = 100.0 + (i % 200) * 0.5
        events.append(_event(t0, f"T{i:06d}", "PAYMENT_INITIATED", 1, t))
        events.append(_event(t0, f"T{i:06d}", "PAYMENT_TIMED_OUT", 2, t + 15.0, t + 3615.0))
    events.sort(key=lambda e: (e.event_timestamp, e.event_id))
    j = 0
    for k in range(420):
        clock.set_time(t0 + timedelta(seconds=k + 1))
        rate = 30.0 if k < 120 else 45.0 if k < 270 else 20.0
        began = time.perf_counter()
        assert core.ingest_telemetry(_window(t0, k, rate)).accepted
        tel_ms.append((time.perf_counter() - began) * 1000.0)
        while j < len(events) and events[j].event_timestamp <= clock.now():
            began = time.perf_counter()
            assert core.ingest_event(events[j]).accepted
            ev_ms.append((time.perf_counter() - began) * 1000.0)
            j += 1
    phases: dict[str, list[float]] = {}
    sizes, sources = [], []
    for _ in range(repeats):
        req = DecisionRequest(
            schema_version=SCHEMA_VERSION,
            experiment_id=EXP,
            route_id=ROUTE,
            requested_at=clock.now(),
        )
        decision, refusal, record = core.decide(encode(req))
        assert refusal is None
        sizes.append(record.response_bytes)
        sources.append(record.source)
        for name, ms in record.timings_ms:
            phases.setdefault(name, []).append(ms)
    summary = {
        name: {"median_ms": statistics.median(v), "p95_ms": _pct(v, 0.95), "max_ms": max(v)}
        for name, v in phases.items()
    }
    return {
        "pending": n,
        "repeats": repeats,
        "phases": summary,
        "telemetry_intake_ms": {"median": statistics.median(tel_ms), "p95": _pct(tel_ms, 0.95)},
        "event_intake_ms": {"median": statistics.median(ev_ms), "p95": _pct(ev_ms, 0.95)},
        "response_bytes_max": max(sizes),
        "decision_reason": decision.reason_code.value,
        "budget_rps": decision.budget_rps,
        "selected": len(decision.selected_transaction_ids),
        "fallback_or_error_rate": sum(s != "optimizer" for s in sources) / len(sources),
        "budget_ms": BUDGETS_MS.get(n),
        "within_budget": summary["total"]["p95_ms"] <= BUDGETS_MS.get(n, float("inf")),
    }


def main() -> None:  # pragma: no cover - evidence generator
    out = Path("results/processed/stage09")
    out.mkdir(parents=True, exist_ok=True)
    results = [run_size(n) for n in POOL_SIZES]
    report = {
        "label": "Stage 09 controller benchmark (local machine, development)",
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "results": results,
    }
    (out / "controller_benchmark.json").write_text(json.dumps(report, indent=2) + "\n")
    for r in results:
        t = r["phases"]["total"]
        print(
            r["pending"],
            f"total median {t['median_ms']:.1f} ms p95 {t['p95_ms']:.1f} ms",
            f"bytes {r['response_bytes_max']}",
            r["decision_reason"],
            r["budget_rps"],
            r["selected"],
            "within" if r["within_budget"] else "OVER BUDGET",
        )


if __name__ == "__main__":  # pragma: no cover
    main()
