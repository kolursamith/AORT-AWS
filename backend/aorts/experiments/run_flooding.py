"""E4 status-check flooding (+ E3/E7 aspects): defenses off vs on, calibrated attack stages.

Scenarios per repeat and attack rate:
  * same_route : A3 floods route B; A1/A2 check their own route-B transactions.
  * mixed      : A3 floods route B; A1/A2 traffic goes to the healthy Bank C.
Concurrent synthetic payments (A1 -> B and C) measure payment impact. Legit status
checks continue for ``tail_s`` after the attack stops (post-attack recovery, E7).

    python -m backend.aorts.experiments.run_flooding --mode inprocess --repeats 5 --rates 10 50 100
"""

from __future__ import annotations

import threading
import time

from backend.aorts.app.metrics import percentile
from backend.aorts.contracts.requests import DefenseConfig
from backend.aorts.control.traffic import TrafficConfig, TrafficGenerator
from backend.aorts.experiments.common import (
    env_info,
    experiment_id,
    http_client,
    inprocess_stack,
    parser,
    write_outputs,
)
from backend.aorts.security.flooding_simulator import FloodConfig, run_flood


def build_pool(client, admin_token: str, plan: dict[str, str], per_bank: int = 5) -> dict:
    import uuid

    pool = {}
    for bank, dest in plan.items():
        pool[bank] = []
        for i in range(per_bank):
            body = {
                "sender_account_id": f"{bank}-CUST-{i + 1:04d}",
                "beneficiary_account_id": f"{dest}-CUST-{i + 1:04d}",
                "beneficiary_bank_id": dest,
                "amount_paise": 1000 + i,
                "idempotency_key": f"fl-{uuid.uuid4().hex}",
            }
            r = client.post(
                "remitter",
                "/api/v1/payments",
                body,
                as_identity="CHANNEL",
                headers={"X-AORTS-Simulate": "lost-response", "X-AORTS-Admin-Token": admin_token},
            )
            r.raise_for_status()
            t = r.json()["transaction"]
            pool[bank].append((t["transaction_id"], t["route_id"]))
    return pool


def one_run(
    client,
    settings,
    set_defenses,
    reset_switch,
    *,
    rate,
    enabled,
    scenario,
    seed,
    duration,
    tail,
    lead=0.0,
):
    set_defenses(enabled)
    reset_switch()
    legit_dest = "B" if scenario == "same_route" else "C"
    pool = build_pool(
        client,
        settings.admin_token.get_secret_value(),
        {"A3": "B", "A1": legit_dest, "A2": legit_dest},
    )
    traffic = TrafficGenerator(
        client,
        TrafficConfig(
            rps=4,
            duration_s=lead + duration + tail,
            banks=("A1",),
            destinations=("B", "C"),
            retry_probability=0.0,
            seed=seed,
        ),
    )
    t_res: dict = {}
    th = threading.Thread(target=lambda: t_res.update(traffic.run()))
    th.start()
    cfg = FloodConfig(
        attack_rps=rate, duration_s=duration, seed=seed, tail_s=tail, destination="B", lead_s=lead
    )
    result = run_flood(client, pool, cfg)
    th.join()
    summ = result.summary(bank_limit=settings.bank_check_rate_per_sec)
    legit = [r for r in result.records if r.bank != "A3"]
    during = [r.latency_ms for r in legit if lead <= r.t <= lead + duration and r.status == 200]
    after = [r.latency_ms for r in legit if r.t > lead + duration and r.status == 200]
    a3 = summ["per_bank"].get("A3", {})
    row = {
        "scenario": scenario,
        "attack_rps": rate,
        "defenses": "on" if enabled else "off",
        "seed": seed,
        "a3_sent": a3.get("sent"),
        "a3_admitted": a3.get("admitted_ok"),
        "a3_admitted_ratio": a3.get("admitted_ratio"),
        "a1_admitted_ratio": summ["per_bank"].get("A1", {}).get("admitted_ratio"),
        "a2_admitted_ratio": summ["per_bank"].get("A2", {}).get("admitted_ratio"),
        "legit_success_ratio": summ["legit_success_ratio"],
        "legit_share_of_admitted": summ["legit_share_of_admitted"],
        "legit_check_p95_during_ms": _r(percentile(during, 0.95)),
        "legit_check_p95_after_ms": _r(percentile(after, 0.95)),
        "detection_s": summ["attack_detection_s"],
        "containment_s": summ["attack_containment_s"],
        "payments_sent": t_res.get("sent"),
        "payments_closed": (t_res.get("outcomes") or {}).get("CLOSED", 0),
        "payment_p95_ms": t_res.get("latency_p95_ms"),
        "baseline_legit_ms": summ.get("baseline_legit_ms"),
        "recovery_after_attack_s": summ.get("recovery_after_attack_s"),
    }
    return row, {"summary": summ, "traffic": t_res}


def _r(x):
    return None if x is None else round(x, 2)


def main() -> None:
    p = parser("E4 status-check flooding experiment")
    p.add_argument("--rates", type=float, nargs="+", default=[10, 50, 100])
    p.add_argument("--duration", type=float, default=5.0)
    p.add_argument("--tail", type=float, default=2.0)
    p.add_argument(
        "--lead",
        type=float,
        default=0.0,
        help="baseline seconds of legit-only traffic before the attack",
    )
    p.add_argument(
        "--core-workers", type=int, default=None, help="calibrate switch capacity (in-process mode)"
    )
    p.add_argument(
        "--base-ms", type=int, default=None, help="switch base processing time per request (ms)"
    )
    p.add_argument("--label", default="")
    p.add_argument(
        "--check-workers", type=int, default=0, help="status-check bulkhead size (0 = shared pool)"
    )
    p.add_argument("--scenarios", nargs="+", default=["same_route", "mixed"])
    args = p.parse_args()
    exp = experiment_id("flooding" + (f"-{args.label}" if args.label else ""))
    p_bulk = args.check_workers
    overrides = {
        k: v
        for k, v in (
            ("switch_core_workers", args.core_workers),
            ("switch_base_processing_ms", args.base_ms),
        )
        if v is not None
    }
    overrides["switch_check_workers"] = p_bulk  # pinned: never inherited from .env
    rows, details = [], []
    started = time.time()

    def run_all(client, settings, set_defenses, reset_switch):
        for rep in range(args.repeats):
            for scenario in args.scenarios:
                for rate in args.rates:
                    for enabled in (False, True):
                        row, det = one_run(
                            client,
                            settings,
                            set_defenses,
                            reset_switch,
                            rate=rate,
                            enabled=enabled,
                            scenario=scenario,
                            seed=args.seed + rep,
                            duration=args.duration,
                            tail=args.tail,
                            lead=args.lead,
                        )
                        row["repeat"] = rep
                        rows.append(row)
                        details.append({**row, "detail": det})
                        print(
                            {
                                k: row[k]
                                for k in (
                                    "scenario",
                                    "attack_rps",
                                    "defenses",
                                    "a3_admitted",
                                    "a1_admitted_ratio",
                                    "legit_check_p95_during_ms",
                                    "containment_s",
                                )
                            },
                            flush=True,
                        )

    if args.mode == "inprocess":
        with inprocess_stack(**overrides) as st:

            def set_def(on):
                st.set_defenses(DefenseConfig(rate_limiting=on, tier2_credits=on))

            def reset_sw():
                st.switch_service.admission.reset()
                st.switch_service.ctx.metrics.reset()

            run_all(st.client, st.settings, set_def, reset_sw)
            ledger = st.ledger.verify_ledger()
    else:
        client, settings = http_client()
        admin = {"X-AORTS-Admin-Token": settings.admin_token.get_secret_value()}

        def set_def(on):
            client.post(
                "control",
                "/api/v1/admin/defenses",
                {
                    "rate_limiting": on,
                    "tier2_credits": on,
                    "telemetry_validation": True,
                    "replay_protection": True,
                },
                as_identity="LAB",
                headers=admin,
            )
            time.sleep(1.2)

        def reset_sw():
            client.post(
                "switch", "/api/v1/admin/security/reset", {}, as_identity="LAB", headers=admin
            )

        run_all(client, settings, set_def, reset_sw)
        set_def(True)
        ledger = None
    payload = {
        "experiment_id": exp,
        "scenario": "E4_flooding",
        "args": vars(args),
        "environment": env_info(args.mode),
        "started_at": started,
        "ended_at": time.time(),
        "ledger_verification": ledger,
        "runs": details,
    }
    print(write_outputs(exp, payload, rows))


if __name__ == "__main__":
    main()
