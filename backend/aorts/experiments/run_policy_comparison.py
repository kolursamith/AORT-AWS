"""Same workload, different recovery policies (Weakness E). In-process stack, real dispatcher.

Timeline per run: ``--degraded`` s with Bank B slow + failing (credits AND status queries),
then ``--restored`` s healthy, then drain (max ``--drain`` s). Payments at ``--rps`` to B and C.
Each policy runs in two arms:
  * raw     - Track B's integrity guard off and a very high hard cap (the policy's own behaviour);
  * guarded - Track B BudgetGuard on (hard cap, switch-measured headroom, telemetry integrity).
Admission control (rate limits, Tier 2 credits) is ON in both arms: banks cannot opt out of it.

Track A's AORTS controller is compared by adding ``--policies http`` with ``AORTS_CONTROLLER_BASE_URL``.

    python -m backend.aorts.experiments.run_policy_comparison --repeats 3
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime

from sqlalchemy import text

from backend.aorts.adapters.baseline_policies import POLICIES, make_policy
from backend.aorts.adapters.controller_client import HttpController, ResilientController
from backend.aorts.adapters.twin_state import PostgresVersionedStore
from backend.aorts.app.metrics import percentile
from backend.aorts.banking.dispatcher.local_adapter import LocalQueue
from backend.aorts.banking.dispatcher.service import Dispatcher
from backend.aorts.contracts.requests import DefenseConfig, FaultConfig
from backend.aorts.control.telemetry_service import HonestProbe
from backend.aorts.control.traffic import TrafficConfig, TrafficGenerator
from backend.aorts.experiments.common import (
    env_info,
    experiment_id,
    inprocess_stack,
    parser,
    write_outputs,
)
from backend.aorts.experiments.run_end_to_end import stable_zero_time

# Ledger backoff = 0 so the POLICY alone decides when a transaction is re-checked.
STACK = dict(
    deadline_seconds=30,
    check_backoff_base_seconds=0.0,
    check_backoff_max_seconds=0.0,
    max_checks_per_transaction=60,
    dispatch_tick_seconds=0.25,
    switch_core_workers=4,
    switch_check_workers=0,
    switch_base_processing_ms=20,
    switch_beneficiary_timeout_s=1.0,
    reconciliation_interval_seconds=0,
)
DEGRADE = FaultConfig(
    target="beneficiary:B",
    latency_ms=200,
    error_rate=0.6,
    drop_response_rate=0.4,
    note="policy comparison: Bank B degraded",
)
SLO_MS = 2000.0


def one_run(policy: str, guarded: bool, seed: int, args) -> dict:
    overrides = dict(STACK, max_budget_per_route=20 if guarded else 10_000, random_seed=seed)
    with inprocess_stack(**overrides) as st:
        st.set_defenses(DefenseConfig(telemetry_validation=guarded))
        primary = (
            HttpController(st.settings.controller_base_url, st.settings.controller_timeout_seconds)
            if policy == "http"
            else make_policy(policy, seed)
        )
        controller = ResilientController(primary, st.settings.fallback_budget_per_route)
        twin = PostgresVersionedStore(st.db)
        dispatcher = Dispatcher(
            st.contexts["remitter"],
            st.remitter_service,
            controller=controller,
            twin=twin,
            queue=LocalQueue(workers=8),
        )
        probe = HonestProbe(
            st.control.app.state.telemetry, ["A1-B", "A1-C", "A2-B", "A2-C"], interval_s=1.0
        )
        threading.Thread(target=dispatcher.run_forever, daemon=True).start()
        probe.start()
        st.set_fault(DEGRADE)
        traffic = TrafficGenerator(
            st.client,
            TrafficConfig(
                rps=args.rps,
                duration_s=args.degraded + args.restored,
                banks=("A1", "A2"),
                destinations=("B", "C"),
                retry_probability=0.0,
                seed=seed,
            ),
        )
        lat: list[tuple[float, float, str]] = []
        orig = traffic._one

        def timed(body, retry=False):  # capture per-payment latency + state for goodput
            t = time.perf_counter()
            r = st.client.post("remitter", "/api/v1/payments", body, as_identity="CHANNEL")
            state = r.json()["transaction"]["state"] if r.status_code == 200 else str(r.status_code)
            lat.append((time.perf_counter() - t0, (time.perf_counter() - t) * 1000, state))

        traffic._one = timed
        t0 = time.perf_counter()
        th = threading.Thread(target=traffic.run)
        th.start()
        samples = []
        restored_at = None
        while th.is_alive():
            el = time.perf_counter() - t0
            if restored_at is None and el >= args.degraded:
                st.reset_faults()
                restored_at = el
            samples.append(
                {"t": round(el, 2), "ambiguous_B": _backlog(st, "B"), "ambiguous_all": _backlog(st)}
            )
            time.sleep(0.5)
        drain_end = time.perf_counter() + args.drain
        while time.perf_counter() < drain_end and _backlog(st) > 0:
            samples.append(
                {
                    "t": round(time.perf_counter() - t0, 2),
                    "ambiguous_B": _backlog(st, "B"),
                    "ambiguous_all": _backlog(st),
                }
            )
            time.sleep(0.5)
        for _ in range(3):  # short stability window so "stays at zero" can be observed
            samples.append(
                {
                    "t": round(time.perf_counter() - t0, 2),
                    "ambiguous_B": _backlog(st, "B"),
                    "ambiguous_all": _backlog(st),
                }
            )
            time.sleep(0.5)
        dispatcher.stop()
        probe.stop()
        time.sleep(0.6)
        traffic._one = orig
        row = _metrics(st, policy, guarded, seed, lat, samples, restored_at, args)
        row["ledger_ok"] = st.ledger.verify_ledger()["ok"]
        return row


def _backlog(st, dest: str | None = None) -> int:
    sql = "SELECT count(*) FROM transactions WHERE state IN ('AMBIGUOUS','CHECK_IN_FLIGHT')"
    if dest:
        sql += f" AND beneficiary_bank_id = '{dest}'"
    with st.db.session() as s:
        return int(s.execute(text(sql)).scalar())


def _metrics(st, policy, guarded, seed, lat, samples, restored_at, args) -> dict:
    with st.db.session() as s:
        rows = (
            s.execute(
                text(
                    "SELECT state, financial_outcome, checks_used, ambiguous_since, resolved_at, deadline_at "
                    "FROM transactions WHERE ambiguous_since IS NOT NULL"
                )
            )
            .mappings()
            .all()
        )
    res = [
        (r["resolved_at"] - r["ambiguous_since"]).total_seconds() for r in rows if r["resolved_at"]
    ]
    in_deadline = sum(1 for r in rows if r["resolved_at"] and r["resolved_at"] <= r["deadline_at"])
    c = st.switch_service.ctx.metrics.counters

    def tot(name):
        return int(sum(v for k, v in c.items() if k == name or k.startswith(name + "{")))

    duration = args.degraded + args.restored
    good = sum(1 for _, ms, state in lat if state == "CLOSED" and ms <= SLO_MS)
    pay_ms = [ms for _, ms, _ in lat]
    rec = stable_zero_time(samples, restored_at or args.degraded, key="ambiguous_all")
    return {
        "policy": policy,
        "arm": "guarded" if guarded else "raw",
        "seed": seed,
        "payments": len(lat),
        "ambiguous_txns": len(rows),
        "resolved": len(res),
        "escalated": sum(1 for r in rows if r["state"] == "HANDOFF_UDIR"),
        "still_unresolved": sum(1 for r in rows if r["state"] in ("AMBIGUOUS", "CHECK_IN_FLIGHT")),
        "deadline_compliance": round(in_deadline / len(rows), 4) if rows else None,
        "resolution_p50_s": _r(percentile(res, 0.5)),
        "resolution_p95_s": _r(percentile(res, 0.95)),
        "checks_per_ambiguous_txn": round(sum(r["checks_used"] for r in rows) / len(rows), 3)
        if rows
        else None,
        "checks_received": tot("check_received"),
        "checks_rejected_429": tot("check_rejected"),
        "checks_forwarded_downstream": tot("check_forwarded"),
        "core_overloaded": tot("core_overloaded"),
        "payment_goodput_per_s": round(good / duration, 3),
        "payment_p95_ms": _r(percentile(pay_ms, 0.95)),
        "backlog_peak": max((x["ambiguous_all"] for x in samples), default=0),
        "recovery_time_s": _r(rec - restored_at)
        if (rec is not None and restored_at is not None)
        else None,
    }


def _r(x):
    return None if x is None else round(x, 3)


def main() -> None:
    p = parser("Recovery policy comparison")
    p.add_argument("--policies", nargs="+", default=list(POLICIES) + ["mock"])
    p.add_argument("--arms", nargs="+", default=["raw", "guarded"])
    p.add_argument("--rps", type=float, default=12.0)
    p.add_argument("--degraded", type=float, default=15.0)
    p.add_argument("--restored", type=float, default=8.0)
    p.add_argument("--drain", type=float, default=30.0)
    p.set_defaults(repeats=3)
    args = p.parse_args()
    exp = experiment_id("policy-comparison")
    rows = []
    started = datetime.now(UTC).isoformat()
    for rep in range(args.repeats):
        for policy in args.policies:
            for arm in args.arms:
                row = one_run(policy, arm == "guarded", args.seed + rep, args)
                row["repeat"] = rep
                rows.append(row)
                print(
                    {
                        k: row[k]
                        for k in (
                            "policy",
                            "arm",
                            "ambiguous_txns",
                            "resolved",
                            "escalated",
                            "checks_received",
                            "checks_rejected_429",
                            "core_overloaded",
                            "resolution_p95_s",
                            "payment_goodput_per_s",
                            "recovery_time_s",
                            "ledger_ok",
                        )
                    },
                    flush=True,
                )
    payload = {
        "experiment_id": exp,
        "scenario": "policy_comparison",
        "args": vars(args),
        "stack_overrides": STACK,
        "fault": DEGRADE.model_dump(),
        "slo_ms": SLO_MS,
        "environment": env_info("inprocess"),
        "started_at": started,
        "note": "baselines are reference policies, not the AORTS controller; "
        "'npci-static' is a simplified simulation schedule, not a regulatory rule",
        "runs": rows,
    }
    print(write_outputs(exp, payload, rows))


if __name__ == "__main__":
    main()
