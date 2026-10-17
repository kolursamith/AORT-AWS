"""E1 normal -> E2/E3 Bank B degraded (Bank C healthy) -> recovery, with the dispatcher resolving
ambiguous payments through real signed status checks (E8).

    python -m backend.aorts.experiments.run_end_to_end --mode inprocess --phase 15
    python -m backend.aorts.experiments.run_end_to_end --mode http          # against `docker compose up`
"""

from __future__ import annotations

import threading
import time
from contextlib import nullcontext

from backend.aorts.control.traffic import TrafficConfig, TrafficGenerator
from backend.aorts.experiments.common import (
    env_info,
    experiment_id,
    http_client,
    inprocess_stack,
    parser,
    write_outputs,
)


def sample(client, admin, t0, phase, samples):
    s = client.get("control", "/api/v1/metrics/summary", as_identity="LAB").json()
    routes = {r["route_id"]: r for r in s["payments"]["by_route"]}
    samples.append(
        {
            "t": round(time.time() - t0, 1),
            "phase": phase,
            "total": s["payments"]["total"],
            "ambiguous_backlog": s["payments"]["ambiguous_backlog"],
            "ambiguous_B": sum(r["ambiguous"] or 0 for k, r in routes.items() if k.endswith("-B")),
            "ambiguous_C": sum(r["ambiguous"] or 0 for k, r in routes.items() if k.endswith("-C")),
            "successful": s["payments"]["successful"],
            "failed_or_reversed": s["payments"]["failed_or_reversed"],
            "escalated": s["payments"]["escalated"],
            "checks_admitted": s["status_checks"]["admitted"],
            "checks_rejected": s["status_checks"]["rejected"],
            "ledger_ok": s["ledger_integrity"]["ok"],
        }
    )
    return s


def stable_zero_time(samples: list[dict], start: float, key: str = "ambiguous_B") -> float | None:
    """Recovery time definition: first sample at/after ``start`` from which ``key`` STAYS 0 until the end
    of the observation (stability window = rest of the run, at least 2 samples)."""
    after = [x for x in samples if x["t"] >= start]
    for i, x in enumerate(after):
        if len(after) - i >= 2 and all(y[key] == 0 for y in after[i:]):
            return x["t"]
    return None


def main() -> None:
    p = parser("E1/E2/E3/E8 end-to-end scenario")
    p.add_argument("--phase", type=float, default=15.0, help="seconds per phase")
    p.add_argument("--rps", type=float, default=5.0)
    args = p.parse_args()
    exp = experiment_id("end-to-end")
    samples: list[dict] = []

    ctx = (
        inprocess_stack(
            dispatch_tick_seconds=0.5, check_backoff_base_seconds=0.5, check_backoff_max_seconds=4.0
        )
        if args.mode == "inprocess"
        else nullcontext()
    )
    with ctx as st:
        if st is not None:
            from backend.aorts.adapters.twin_state import PostgresVersionedStore
            from backend.aorts.banking.dispatcher.service import Dispatcher

            client, settings = st.client, st.settings
            dispatcher = Dispatcher(
                st.contexts["remitter"], st.remitter_service, twin=PostgresVersionedStore(st.db)
            )
            threading.Thread(target=dispatcher.run_forever, daemon=True).start()
            from backend.aorts.control.telemetry_service import (
                HonestProbe,  # the twin's honest sensor
            )

            probe = HonestProbe(
                st.control.app.state.telemetry, ["A1-B", "A1-C", "A2-B", "A2-C"], interval_s=1.0
            )
            probe.start()
        else:
            client, settings = http_client()
            dispatcher = probe = None
        admin = {"X-AORTS-Admin-Token": settings.admin_token.get_secret_value()}

        def fault(body):
            client.post("control", "/api/v1/admin/faults", body, as_identity="LAB", headers=admin)

        def reset():
            client.post(
                "control", "/api/v1/admin/faults/reset", {}, as_identity="LAB", headers=admin
            )

        reset()
        traffic = TrafficGenerator(
            client,
            TrafficConfig(
                rps=args.rps,
                duration_s=args.phase * 3,
                banks=("A1", "A2"),
                destinations=("B", "C"),
                seed=args.seed,
                retry_probability=0.2,
            ),
            correlation_id=exp,
        )
        t_result: dict = {}
        th = threading.Thread(target=lambda: t_result.update(traffic.run()))
        t0 = time.time()
        th.start()
        marks = {}
        for phase in ("E1_normal", "E2_E3_bank_B_degraded", "recovery"):
            marks[phase] = round(time.time() - t0, 1)
            if phase == "E2_E3_bank_B_degraded":
                fault(
                    {
                        "target": "beneficiary:B",
                        "latency_ms": 300,
                        "drop_response_rate": 0.5,
                        "note": "E2: Bank B slow + lost responses",
                    }
                )
            elif phase == "recovery":
                reset()
            end = time.time() + args.phase
            while time.time() < end:
                sample(client, admin, t0, phase, samples)
                time.sleep(1.0)
        th.join()
        # Drain: let the dispatcher resolve the remaining backlog (bounded wait).
        drain_end = time.time() + max(30.0, args.phase * 2)
        while time.time() < drain_end:
            s = sample(client, admin, t0, "drain", samples)
            if s["payments"]["ambiguous_backlog"] == 0:
                break
            time.sleep(1.0)
        final = client.get("control", "/api/v1/metrics/summary", as_identity="LAB").json()
        if dispatcher:
            dispatcher.stop()
        if probe:
            probe.stop()

    rec_start = marks["recovery"]
    recovered_at = stable_zero_time(samples, rec_start)
    degraded = [x for x in samples if x["phase"] == "E2_E3_bank_B_degraded"]
    result = {
        "phase_marks_s": marks,
        "traffic": t_result,
        "max_ambiguous_B_during_degradation": max(
            (x["ambiguous_B"] for x in degraded), default=None
        ),
        "max_ambiguous_C_during_degradation": max(
            (x["ambiguous_C"] for x in degraded), default=None
        ),
        "backlog_clear_time_after_fault_removed_s": (
            round(recovered_at - rec_start, 1) if recovered_at else None
        ),
        "final_payments": final["payments"],
        "resolution_latency_s": final["resolution_latency_s"],
        "status_checks": final["status_checks"],
        "ledger_integrity": final["ledger_integrity"],
    }
    payload = {
        "experiment_id": exp,
        "scenario": "E1_E2_E3_E8_end_to_end",
        "args": vars(args),
        "environment": env_info(args.mode),
        "result": result,
        "samples": samples,
    }
    print({k: v for k, v in result.items() if k not in ("traffic", "status_checks")})
    print(write_outputs(exp, payload, samples))


if __name__ == "__main__":
    main()
