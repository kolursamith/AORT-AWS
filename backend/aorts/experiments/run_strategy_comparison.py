"""Compare AORTS with baseline recovery strategies on identical seeded workloads (requirements 4, 5, 7).

For every (scenario, seed) each strategy runs on a FRESH database with the same seed, the same
traffic sequence, the same fault timeline and the same attacks, and through the SAME Track B safety
layers (BudgetGuard, PreDispatchGate, admission control, primary-capacity reservation, bulkhead and the
execution-time FinancialSafetyGate). Results are paired by seed.

    python -m backend.aorts.experiments.run_strategy_comparison --scenario S1-degraded-B --seeds 30
    python -m backend.aorts.experiments.analyze_strategy_comparison results/raw/strategy-*.jsonl
"""

from __future__ import annotations

import argparse
import json
import logging
import threading
import time
import uuid
from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import text

from backend.aorts.adapters.baseline_policies import make_policy
from backend.aorts.adapters.controller_client import ResilientController
from backend.aorts.adapters.twin_state import PostgresVersionedStore
from backend.aorts.app.metrics import percentile
from backend.aorts.banking.dispatcher.local_adapter import LocalQueue
from backend.aorts.banking.dispatcher.service import Dispatcher
from backend.aorts.banking.ledger.safety_gate import GATE_STATS
from backend.aorts.contracts.events import new_id
from backend.aorts.contracts.requests import FaultConfig, RouteTelemetry
from backend.aorts.control.telemetry_service import HonestProbe
from backend.aorts.control.traffic import TrafficConfig, TrafficGenerator
from backend.aorts.experiments.common import RESULTS, inprocess_stack
from backend.aorts.experiments.provenance import provenance
from backend.aorts.recovery.budget import BudgetConfig
from backend.aorts.recovery.controller import AortsController
from backend.aorts.security.flooding_simulator import FloodConfig, run_flood
from backend.aorts.twin.engine import TwinEngine
from backend.aorts.workloads.scenarios import SCENARIOS, Scenario

STRATEGIES = ["static-polling", "exp-backoff-jitter", "fixed-budget", "aimd", "aorts"]
STACK = dict(
    deadline_seconds=30,
    check_backoff_base_seconds=0.0,
    check_backoff_max_seconds=0.0,
    max_checks_per_transaction=60,
    dispatch_tick_seconds=0.5,
    switch_core_workers=8,
    switch_check_workers=3,
    switch_workers_per_destination=6,
    switch_base_processing_ms=10,
    switch_beneficiary_timeout_s=1.0,
    switch_primary_reserve_fraction=0.25,
    max_budget_per_route=20,
    max_inflight_checks_per_route=10,
    reconciliation_interval_seconds=5,
    fallback_budget_per_route=2,
)
SLO_MS = 2000.0
DRAIN_S = 20.0


class DuplicatingQueue(LocalQueue):
    """At-least-once delivery: each job is (seeded-randomly) delivered twice."""

    def __init__(self, prob: float, seed: int, workers: int = 8):
        super().__init__(workers=workers)
        import random

        self.prob, self.rng, self.duplicates = prob, random.Random(seed), 0

    def send(self, job):
        super().send(job)
        if self.rng.random() < self.prob:
            self.duplicates += 1
            super().send(job)


def make_strategy(name: str, st, store, seed: int):
    if name == "aorts":
        return AortsController(
            TwinEngine(st.db),
            store=store,
            budget_cfg=BudgetConfig(
                tick_s=STACK["dispatch_tick_seconds"],
                fallback_budget=STACK["fallback_budget_per_route"],
                hard_cap=STACK["max_budget_per_route"],
            ),
        )
    return make_policy({"static-polling": "npci-static"}.get(name, name), seed)


def _count(st, sql, **p):
    with st.db.session() as s:
        return s.execute(text(sql), p).scalar()


def _backlog(st, dest=None):
    sql = "SELECT count(*) FROM transactions WHERE state IN ('AMBIGUOUS','CHECK_IN_FLIGHT')"
    return int(_count(st, sql + (" AND beneficiary_bank_id=:d" if dest else ""), d=dest))


def one_run(scn: Scenario, strategy: str, seed: int) -> dict:
    GATE_STATS.clear()
    with inprocess_stack(**STACK, random_seed=seed) as st:
        store = PostgresVersionedStore(st.db)
        primary = make_strategy(strategy, st, store, seed)
        queue = DuplicatingQueue(scn.duplicate_job_prob, seed)
        disp = Dispatcher(
            st.contexts["remitter"],
            st.remitter_service,
            controller=ResilientController(primary, 2),
            twin=store,
            queue=queue,
        )
        telemetry = st.control.app.state.telemetry
        # The control service's telemetry service writes into the SAME store the dispatcher's guard reads.
        telemetry.twin = store
        probe = HonestProbe(telemetry, ["A1-B", "A1-C", "A2-B", "A2-C"], interval_s=1.0)
        # Calibrate the no-fault base service time (ground truth for capacity-estimation error).
        cal = [_timed_pay(st, f"cal-{seed}-{i}", "B") for i in range(5)]
        base_ms = percentile([ms for ms, _ in cal], 0.5)
        workers = STACK["switch_workers_per_destination"]
        a3_pool = []
        for i in range(3):
            body = {
                "sender_account_id": f"A3-CUST-000{i + 1}",
                "beneficiary_account_id": "B-CUST-0001",
                "beneficiary_bank_id": "B",
                "amount_paise": 100,
                "idempotency_key": f"a3-{seed}-{i}-{uuid.uuid4().hex[:6]}",
            }
            t = st.client.post("remitter", "/api/v1/payments", body, as_identity="CHANNEL").json()[
                "transaction"
            ]
            a3_pool.append((t["transaction_id"], t["route_id"]))
        threading.Thread(target=disp.run_forever, daemon=True).start()
        probe.start()

        lat: list[tuple[float, float, str]] = []
        traffic = TrafficGenerator(
            st.client,
            TrafficConfig(
                rps=scn.rps,
                duration_s=scn.traffic_s,
                banks=("A1", "A2"),
                destinations=("B", "C"),
                retry_probability=scn.retry_probability,
                burst_every_s=scn.burst_every_s,
                burst_multiplier=scn.burst_multiplier,
                seed=seed,
            ),
        )
        t0 = time.perf_counter()

        def timed(body, retry=False):
            t = time.perf_counter()
            r = st.client.post("remitter", "/api/v1/payments", body, as_identity="CHANNEL")
            state = r.json()["transaction"]["state"] if r.status_code == 200 else str(r.status_code)
            lat.append((t - t0, (time.perf_counter() - t) * 1000, state))
            if not retry and state == "AMBIGUOUS" and traffic._rng.random() < scn.retry_probability:
                timed(body, retry=True)

        traffic._one = timed
        th = threading.Thread(target=traffic.run)
        th.start()
        samples, flood_results = [], []
        phase_marks = []
        for ph in scn.phases:
            st.reset_faults()
            for f in ph.faults:
                st.contexts["switch"].runtime.set_fault(FaultConfig(**f), "SCENARIO")
            st.refresh_runtime()
            phase_marks.append((ph.name, round(time.perf_counter() - t0, 2)))
            attackers = []
            if ph.flood_rps:

                def flood(rps=ph.flood_rps, dur=ph.duration_s):
                    flood_results.append(
                        run_flood(
                            st.client,
                            {"A3": a3_pool},
                            FloodConfig(
                                attack_rps=rps,
                                duration_s=dur,
                                legit_banks=(),
                                tail_s=0,
                                seed=seed,
                                concurrency=48,
                            ),
                        ).summary(bank_limit=st.settings.bank_check_rate_per_sec)
                    )

                attackers.append(threading.Thread(target=flood))
            if ph.poison:
                attackers.append(
                    threading.Thread(target=_poison, args=(telemetry, ph.duration_s, seed))
                )
            for a in attackers:
                a.start()
            end = time.perf_counter() + ph.duration_s
            b_lat_fault = sum(
                f.get("latency_ms", 0) for f in ph.faults if f["target"] == "beneficiary:B"
            )
            truth_b = workers / ((b_lat_fault + base_ms) / 1000.0)
            while time.perf_counter() < end:
                samples.append(_sample(st, primary, ph.name, t0, truth_b, strategy))
                time.sleep(0.5)
            for a in attackers:
                a.join(timeout=ph.duration_s + 5)
        th.join()
        st.reset_faults()
        restored_at = time.perf_counter() - t0
        drain_end = time.perf_counter() + DRAIN_S
        while time.perf_counter() < drain_end and _backlog(st) > 0:
            samples.append(
                _sample(st, primary, "drain", t0, workers / (base_ms / 1000.0), strategy)
            )
            time.sleep(0.5)
        samples.append(_sample(st, primary, "drain", t0, workers / (base_ms / 1000.0), strategy))
        disp.stop()
        probe.stop()
        time.sleep(0.6)
        row = _metrics(
            st, scn, strategy, seed, lat, samples, flood_results, restored_at, disp, queue, base_ms
        )
        row["phase_marks"] = phase_marks
        return row


def _timed_pay(st, key, dest):
    t = time.perf_counter()
    body = {
        "sender_account_id": "A2-CUST-0010",
        "beneficiary_account_id": f"{dest}-CUST-0010",
        "beneficiary_bank_id": dest,
        "amount_paise": 100,
        "idempotency_key": f"{key}-{uuid.uuid4().hex[:6]}",
    }
    r = st.client.post("remitter", "/api/v1/payments", body, as_identity="CHANNEL")
    return (time.perf_counter() - t) * 1000, r.json()["transaction"]["state"]


def _poison(telemetry, duration, seed):
    """Compromised (registered) probe reporting a wildly healthy, high-capacity route B."""
    end = time.time() + duration
    seq = int(time.time() * 1000) + seed
    while time.time() < end:
        seq += 1
        for route in ("A1-B", "A2-B"):
            obs = RouteTelemetry(
                event_id=new_id("tel"),
                source_id="twin-probe-2",
                route_id=route,
                sequence=seq,
                timestamp=datetime.now(UTC),
                latency_p50_ms=5.0,
                latency_p95_ms=8.0,
                throughput_tps=900.0,
                timeout_rate=0.0,
                backlog=0,
                health_score=1.0,
            )
            telemetry.ingest(obs, authenticated_source="twin-probe-2")
        time.sleep(0.5)


def _sample(st, primary, phase, t0, truth_b, strategy):
    s = {
        "t": round(time.perf_counter() - t0, 2),
        "phase": phase,
        "backlog": _backlog(st),
        "backlog_B": _backlog(st, "B"),
        "truth_capacity_B_rps": round(truth_b, 2),
    }
    if strategy == "aorts" and getattr(primary, "last_decision", None):
        routes = primary.last_decision["predicted"]["routes"]
        r = routes.get("A1-B") or routes.get("A2-B")
        if r:
            s.update(
                est_point_B=r["estimate"]["point_rps"],
                est_low_B=r["estimate"]["low_rps"],
                est_conf_B=r["estimate"]["confidence"],
                budget_B=r["decision"]["budget"],
                mode_B=r["decision"]["mode"],
            )
    return s


def _metrics(
    st, scn, strategy, seed, lat, samples, floods, restored_at, disp, queue, base_ms
) -> dict:
    with st.db.session() as s:
        rows = (
            s.execute(
                text(
                    "SELECT state, checks_used, ambiguous_since, resolved_at, deadline_at FROM transactions "
                    "WHERE ambiguous_since IS NOT NULL AND idempotency_key NOT LIKE 'a3-%%' "
                    "AND idempotency_key NOT LIKE 'cal-%%'"
                )
            )
            .mappings()
            .all()
        )
    res = [
        (r["resolved_at"] - r["ambiguous_since"]).total_seconds() for r in rows if r["resolved_at"]
    ]
    c = st.switch_service.ctx.metrics.counters

    def tot(name, **lab):
        key = name + (
            "{" + ",".join(f"{k}={v}" for k, v in sorted(lab.items())) + "}" if lab else ""
        )
        return int(
            sum(v for k, v in c.items() if (k == key) or (not lab and k.startswith(name + "{")))
        )

    ok = [ms for _, ms, state in lat if state == "CLOSED"]
    good = sum(1 for _, ms, state in lat if state == "CLOSED" and ms <= SLO_MS)
    ver = st.ledger.verify_ledger()
    est = [
        x
        for x in samples
        if x.get("est_point_B") is not None and x["phase"].startswith(("degraded", "relapse"))
    ]
    area = 0.0
    for a, b in zip(samples, samples[1:], strict=False):
        area += a["backlog"] * (b["t"] - a["t"])
    flood = floods[0] if floods else {}
    a3 = (flood.get("per_bank") or {}).get("A3", {})
    stable = next(
        (
            x["t"]
            for i, x in enumerate(samples)
            if x["t"] >= restored_at and all(y["backlog"] == 0 for y in samples[i:])
        ),
        None,
    )
    legit_checks = tot("check_received") - (a3.get("sent") or 0)
    return {
        "scenario": scn.name,
        "strategy": strategy,
        "seed": seed,
        "held_out": scn.held_out,
        "payments_sent": len(lat),
        "primary_success_ratio": _r(len(ok) / len(lat)) if lat else None,
        "primary_goodput_per_s": _r(good / scn.traffic_s),
        "primary_p50_ms": _r(percentile(ok, 0.5)),
        "primary_p95_ms": _r(percentile(ok, 0.95)),
        "primary_refused_by_switch": tot("core_overloaded", kind="pay"),
        "ambiguous_txns": len(rows),
        "resolved": len(res),
        "resolution_mean_s": _r(sum(res) / len(res)) if res else None,
        "resolution_p50_s": _r(percentile(res, 0.5)),
        "resolution_p95_s": _r(percentile(res, 0.95)),
        "deadline_compliance": _r(
            sum(1 for r in rows if r["resolved_at"] and r["resolved_at"] <= r["deadline_at"])
            / len(rows)
        )
        if rows
        else None,
        "escalated": sum(1 for r in rows if r["state"] == "HANDOFF_UDIR"),
        "unresolved_at_end": sum(1 for r in rows if r["state"] in ("AMBIGUOUS", "CHECK_IN_FLIGHT")),
        "backlog_peak": max((x["backlog"] for x in samples), default=0),
        "backlog_area": _r(area),
        "recovery_time_s": _r(stable - restored_at) if stable is not None else None,
        "status_checks_legit": legit_checks,
        "status_check_amplification": _r(legit_checks / len(rows)) if rows else None,
        "checks_rejected": tot("check_rejected"),
        "recovery_blocked_by_reserve": tot("recovery_blocked_by_primary_reserve"),
        "duplicate_jobs_injected": queue.duplicates,
        "stale_jobs_dropped": int(
            sum(
                v
                for k, v in disp.ctx.metrics.counters.items()
                if k.startswith("dispatch_stale_jobs")
            )
        ),
        "flood_a3_sent": a3.get("sent"),
        "flood_a3_admitted": a3.get("admitted_ok"),
        "flood_containment_s": flood.get("attack_containment_s"),
        "flood_detection_s": flood.get("attack_detection_s"),
        "capacity_samples": len(est),
        "capacity_rel_err_point": _r(
            sum(
                abs(x["est_point_B"] - x["truth_capacity_B_rps"]) / x["truth_capacity_B_rps"]
                for x in est
            )
            / len(est)
        )
        if est
        else None,
        "capacity_low_overestimate_ratio": _r(
            sum(1 for x in est if x["est_low_B"] and x["est_low_B"] > x["truth_capacity_B_rps"])
            / len(est)
        )
        if est
        else None,
        "base_service_ms": _r(base_ms),
        "invariant_violations": sum(
            len(ver[k])
            for k in (
                "unbalanced_operations",
                "duplicate_operations",
                "balance_mismatches",
                "conservation_violations",
            )
        ),
        "financial_gate": dict(GATE_STATS),
        "predispatch_gate": dict(Counter(disp.gate.stats)),
        "samples": samples,
    }


def _r(x):
    return None if x is None else round(float(x), 4)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--scenario", choices=list(SCENARIOS), required=True)
    p.add_argument("--seeds", type=int, default=30)
    p.add_argument("--first-seed", type=int, default=1000)
    p.add_argument("--strategies", nargs="+", default=STRATEGIES)
    p.add_argument(
        "--mode",
        choices=["docker", "inprocess"],
        default="docker",
        help="docker (default, multi-process, real timeouts) or inprocess (fast smoke runs)",
    )
    args = p.parse_args()
    from backend.aorts.experiments.docker_runner import DockerEnv, one_run_docker

    env = DockerEnv() if args.mode == "docker" else None
    scn = SCENARIOS[args.scenario]
    prov = provenance(
        "strategy-comparison",
        {
            "scenario": scn.as_dict(),
            "mode": args.mode,
            "stack_overrides": STACK
            if args.mode == "inprocess"
            else "docker-compose.experiments.yml",
            "strategies": args.strategies,
            "seeds": args.seeds,
            "first_seed": args.first_seed,
            "slo_ms": SLO_MS,
        },
    )
    (RESULTS / "raw").mkdir(parents=True, exist_ok=True)
    out = RESULTS / "raw" / f"strategy-{prov['run_id']}.jsonl"
    with out.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"provenance": prov}) + "\n")
        consecutive_errors = 0
        for seed in range(args.first_seed, args.first_seed + args.seeds):
            for strat in args.strategies:  # same seed for every strategy -> paired comparison
                if env:
                    _wait_healthy(env)
                t = time.time()
                try:
                    row = (
                        one_run_docker(env, scn, strat, seed) if env else one_run(scn, strat, seed)
                    )
                except Exception as exc:  # noqa: BLE001 - record failures, never drop them silently
                    row = {
                        "scenario": scn.name,
                        "strategy": strat,
                        "seed": seed,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                row["wall_s"] = round(time.time() - t, 1)
                consecutive_errors = consecutive_errors + 1 if "error" in row else 0
                f.write(json.dumps(row, default=str) + "\n")
                f.flush()
                print(
                    {
                        k: row.get(k)
                        for k in (
                            "seed",
                            "strategy",
                            "ambiguous_txns",
                            "resolution_mean_s",
                            "primary_goodput_per_s",
                            "status_check_amplification",
                            "invariant_violations",
                            "error",
                            "wall_s",
                        )
                    },
                    flush=True,
                )
                if consecutive_errors >= 3:
                    f.write(
                        json.dumps({"aborted": "3 consecutive failed runs; stack unhealthy"}) + "\n"
                    )
                    print("ABORTED: 3 consecutive failed runs", flush=True)
                    print(f"raw results: {out}")
                    return
    print(f"raw results: {out}")


def _wait_healthy(env, timeout_s: float = 120.0) -> None:
    """Block until every service answers its health check (the Docker engine can stall under load)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if all(
                env.client.get(t, "/api/v1/health", as_identity="LAB", timeout=5).status_code == 200
                for t in ("remitter", "switch", "control", "beneficiary:B", "beneficiary:C")
            ):
                return
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).debug("best-effort step failed", exc_info=True)
        time.sleep(3)
    raise SystemExit("stack not healthy; aborting campaign")


if __name__ == "__main__":
    main()
