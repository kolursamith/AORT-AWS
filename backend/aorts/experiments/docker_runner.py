"""Docker-mode run of one (scenario, strategy, seed): drives the running experiment stack over HTTP.

Start the stack first:  docker compose -f docker-compose.yml -f docker-compose.experiments.yml up -d
Every service is its own process (real HTTP, real timeouts), unlike the in-process test stack.
"""

from __future__ import annotations

import logging
import random
import threading
import time
import uuid
from datetime import UTC, datetime

from sqlalchemy import text

from backend.aorts.app.metrics import percentile
from backend.aorts.contracts.events import new_id
from backend.aorts.control.traffic import TrafficConfig, TrafficGenerator
from backend.aorts.security.flooding_simulator import FloodConfig, run_flood
from backend.aorts.workloads.scenarios import Scenario

STRATEGY_MODE = {
    "static-polling": "baseline:npci-static",
    "exp-backoff-jitter": "baseline:exp-backoff-jitter",
    "fixed-budget": "baseline:fixed-budget",
    "aimd": "baseline:aimd",
    "aorts": "aorts",
    "naive-polling": "baseline:naive-polling",
}
SLO_MS = 2000.0
DRAIN_S = 20.0


class DockerEnv:
    def __init__(self):
        from backend.aorts.adapters.twin_state import PostgresVersionedStore
        from backend.aorts.app.db import Database
        from backend.aorts.experiments.common import http_client

        self.client, self.settings = http_client()
        self.db = Database(self.settings.database_url.get_secret_value(), pool_size=10)
        self.admin = {"X-AORTS-Admin-Token": self.settings.admin_token.get_secret_value()}
        self.store = PostgresVersionedStore(self.db)

    def ctl(self, path, body=None):
        r = self.client.post(
            "control", path, body or {}, as_identity="LAB", headers=self.admin, timeout=30
        )
        if r.status_code >= 400:
            raise RuntimeError(f"{path}: HTTP {r.status_code} {r.text[:200]}")
        return r.json()

    def sw(self, path, body=None):
        return self.client.post("switch", path, body or {}, as_identity="LAB", headers=self.admin)

    def scalar(self, sql, **p):
        with self.db.session() as s:
            return s.execute(text(sql), p).scalar()

    def backlog(self, dest=None):
        sql = "SELECT count(*) FROM transactions WHERE state IN ('AMBIGUOUS','CHECK_IN_FLIGHT')"
        return int(self.scalar(sql + (" AND beneficiary_bank_id=:d" if dest else ""), d=dest))


def one_run_docker(env: DockerEnv, scn: Scenario, strategy: str, seed: int) -> dict:
    env.ctl("/api/v1/admin/faults/reset")
    env.ctl("/api/v1/admin/reset", {"confirm": "RESET-SYNTHETIC-DATA", "snapshot": False})
    env.sw("/api/v1/admin/security/reset")
    run_id = f"{strategy}-{seed}-{uuid.uuid4().hex[:6]}"
    env.ctl(
        "/api/v1/admin/controller",
        {"mode": STRATEGY_MODE[strategy], "run_id": run_id, "seed": seed},
    )
    deadline = time.time() + 20
    while time.time() < deadline:  # wait until the dispatcher has built the fresh controller
        got = env.store.get("dispatcher:stats")
        if got and got[1].get("run_id") == run_id:
            break
        time.sleep(0.3)
    else:
        raise RuntimeError(
            "dispatcher did not pick up the new controller (is the experiment stack running?)"
        )
    client = env.client
    cal = []
    for i in range(
        5
    ):  # no-fault calibration of the base service time (ground truth for capacity error)
        t = time.perf_counter()
        body = {
            "sender_account_id": "A2-CUST-0010",
            "beneficiary_account_id": "B-CUST-0010",
            "beneficiary_bank_id": "B",
            "amount_paise": 100 + i,
            "idempotency_key": f"cal-{run_id}-{i}",
        }
        client.post("remitter", "/api/v1/payments", body, as_identity="CHANNEL", timeout=10)
        cal.append((time.perf_counter() - t) * 1000)
    base_ms = percentile(cal, 0.5)
    sw = client.get("switch", "/api/v1/metrics", as_identity="LAB").json()
    workers = next((c["workers"] for c in sw.get("capacity", []) if c["destination"] == "B"), 6)
    a3_pool = []
    for i in range(3):
        body = {
            "sender_account_id": f"A3-CUST-000{i + 1}",
            "beneficiary_account_id": "B-CUST-0001",
            "beneficiary_bank_id": "B",
            "amount_paise": 100,
            "idempotency_key": f"a3-{run_id}-{i}",
        }
        t = client.post(
            "remitter", "/api/v1/payments", body, as_identity="CHANNEL", timeout=10
        ).json()["transaction"]
        a3_pool.append((t["transaction_id"], t["route_id"]))

    lat: list[tuple[float, float, str]] = []
    traffic = TrafficGenerator(
        client,
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
    retry_rng = random.Random(f"retry|{seed}")
    t0 = time.perf_counter()

    def timed(body, retry=False):
        t = time.perf_counter()
        try:
            r = client.post("remitter", "/api/v1/payments", body, as_identity="CHANNEL", timeout=15)
            state = r.json()["transaction"]["state"] if r.status_code == 200 else str(r.status_code)
        except Exception as exc:  # noqa: BLE001 - a client-side timeout is a measured failure
            state = type(exc).__name__
        lat.append((t - t0, (time.perf_counter() - t) * 1000, state))
        if not retry and state == "AMBIGUOUS" and retry_rng.random() < scn.retry_probability:
            timed(body, retry=True)  # customer re-submits with the SAME idempotency key

    traffic._one = timed
    th = threading.Thread(target=traffic.run)
    th.start()
    samples, floods, marks = [], [], []
    for ph in scn.phases:
        env.ctl("/api/v1/admin/faults/reset")
        for f in ph.faults:
            env.ctl("/api/v1/admin/faults", f)
        marks.append((ph.name, round(time.perf_counter() - t0, 2)))
        attackers = []
        if ph.flood_rps:

            def flood(rps=ph.flood_rps, dur=ph.duration_s):
                floods.append(
                    run_flood(
                        client,
                        {"A3": a3_pool},
                        FloodConfig(
                            attack_rps=rps,
                            duration_s=dur,
                            legit_banks=(),
                            tail_s=0,
                            seed=seed,
                            concurrency=48,
                        ),
                    ).summary(bank_limit=env.settings.bank_check_rate_per_sec)
                )

            attackers.append(threading.Thread(target=flood))
        if ph.poison:
            attackers.append(threading.Thread(target=_poison, args=(client, ph.duration_s, seed)))
        for a in attackers:
            a.start()
        b_fault = sum(f.get("latency_ms", 0) for f in ph.faults if f["target"] == "beneficiary:B")
        truth_b = workers / ((b_fault + base_ms) / 1000.0)
        end = time.perf_counter() + ph.duration_s
        while time.perf_counter() < end:
            samples.append(_sample(env, ph.name, t0, truth_b, strategy))
            time.sleep(0.5)
        for a in attackers:
            a.join(timeout=ph.duration_s + 10)
    th.join()
    env.ctl("/api/v1/admin/faults/reset")
    restored_at = time.perf_counter() - t0
    healthy_truth = workers / (base_ms / 1000.0)
    drain_end = time.perf_counter() + DRAIN_S
    while time.perf_counter() < drain_end and env.backlog() > 0:
        samples.append(_sample(env, "drain", t0, healthy_truth, strategy))
        time.sleep(0.5)
    for _ in range(2):
        samples.append(_sample(env, "drain", t0, healthy_truth, strategy))
        time.sleep(0.5)
    return _metrics(
        env, scn, strategy, seed, lat, samples, floods, restored_at, base_ms, run_id, marks
    )


def _poison(client, duration, seed):
    """Compromised (registered) probe reporting a wildly healthy, high-capacity route B."""
    end = time.time() + duration
    seq = int(time.time() * 1000) + seed
    while time.time() < end:
        seq += 1
        for route in ("A1-B", "A2-B"):
            obs = {
                "event_id": new_id("tel"),
                "source_id": "twin-probe-2",
                "route_id": route,
                "sequence": seq,
                "timestamp": datetime.now(UTC).isoformat(),
                "latency_p50_ms": 5.0,
                "latency_p95_ms": 8.0,
                "throughput_tps": 900.0,
                "timeout_rate": 0.0,
                "backlog": 0,
                "health_score": 1.0,
            }
            try:
                client.post(
                    "control", "/api/v1/telemetry/route", obs, as_identity="twin-probe-2", timeout=5
                )
            except Exception:  # noqa: BLE001
                logging.getLogger(__name__).debug("best-effort step failed", exc_info=True)
        time.sleep(0.5)


def _sample(env, phase, t0, truth_b, strategy):
    s = {
        "t": round(time.perf_counter() - t0, 2),
        "phase": phase,
        "backlog": env.backlog(),
        "backlog_B": env.backlog("B"),
        "truth_capacity_B_rps": round(truth_b, 2),
    }
    if strategy == "aorts":
        snap = env.store.get("aorts:snapshot")
        dec = (snap[1].get("last_decision") or {}) if snap else {}
        routes = (dec.get("predicted") or {}).get("routes") or {}
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


def _r(x):
    return None if x is None else round(float(x), 4)


def _metrics(
    env, scn, strategy, seed, lat, samples, floods, restored_at, base_ms, run_id, marks
) -> dict:
    from backend.aorts.banking.ledger.service import LedgerService

    with env.db.session() as s:
        rows = (
            s.execute(
                text(
                    "SELECT state, checks_used, ambiguous_since, resolved_at, deadline_at FROM transactions "
                    "WHERE ambiguous_since IS NOT NULL AND idempotency_key NOT LIKE 'a3-%' "
                    "AND idempotency_key NOT LIKE 'cal-%'"
                )
            )
            .mappings()
            .all()
        )
        gate_rejections = s.execute(
            text("SELECT count(*) FROM audit_events WHERE reason LIKE '%financial safety gate%'")
        ).scalar()
        gate_passes = s.execute(
            text(
                "SELECT count(*) FROM audit_events WHERE action LIKE '%COMMITTED' "
                "AND metadata::text LIKE '%safety_gate%'"
            )
        ).scalar()
    counters = (
        env.client.get("switch", "/api/v1/metrics", as_identity="LAB").json().get("counters", {})
    )
    dstats = (env.store.get("dispatcher:stats") or (0, {}))[1]
    res = [
        (r["resolved_at"] - r["ambiguous_since"]).total_seconds() for r in rows if r["resolved_at"]
    ]

    def tot(name, **lab):
        key = name + (
            "{" + ",".join(f"{k}={v}" for k, v in sorted(lab.items())) + "}" if lab else ""
        )
        return int(
            sum(
                v for k, v in counters.items() if k == key or (not lab and k.startswith(name + "{"))
            )
        )

    ok = [ms for _, ms, st_ in lat if st_ == "CLOSED"]
    good = sum(1 for _, ms, st_ in lat if st_ == "CLOSED" and ms <= SLO_MS)
    ver = LedgerService(env.db).verify_ledger()
    est = [
        x
        for x in samples
        if x.get("est_point_B") is not None and x["phase"].startswith(("degraded", "relapse"))
    ]
    area = sum(a["backlog"] * (b["t"] - a["t"]) for a, b in zip(samples, samples[1:], strict=False))
    flood = floods[0] if floods else {}
    a3 = (flood.get("per_bank") or {}).get("A3", {})
    a3_checks = sum(
        int(v) for k, v in counters.items() if k.startswith("check_received{") and "bank=A3" in k
    )
    legit_checks = tot("check_received") - a3_checks
    stable = next(
        (
            x["t"]
            for i, x in enumerate(samples)
            if x["t"] >= restored_at and all(y["backlog"] == 0 for y in samples[i:])
        ),
        None,
    )
    return {
        "scenario": scn.name,
        "strategy": strategy,
        "seed": seed,
        "run_id": run_id,
        "held_out": scn.held_out,
        "mode": "docker",
        "phase_marks": marks,
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
        "duplicate_jobs_injected": dstats.get("duplicate_jobs_injected"),
        "stale_jobs_dropped": dstats.get("stale_jobs_dropped"),
        "predispatch_gate": dstats.get("gate"),
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
        "financial_gate_passes": gate_passes,
        "financial_gate_rejections": gate_rejections,
        "samples": samples,
    }
