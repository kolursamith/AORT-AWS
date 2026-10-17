"""Controlled attack experiments, launched from the dashboard or the CLI scripts.

Only available when ``AORTS_ENABLE_ATTACK_CONTROLS=true`` and the caller holds the
admin token. Targets are checked against the isolation allow-list.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime

from backend.aorts.contracts.events import new_id
from backend.aorts.contracts.requests import AttackRunRequest, DefenseConfig, RouteTelemetry
from backend.aorts.security.attack_config import assert_isolated_target
from backend.aorts.security.flooding_simulator import FloodConfig, run_flood
from backend.aorts.security.poisoning_simulator import CASES, PoisonConfig, run_poisoning
from backend.aorts.security.replay_simulator import ReplaySimulator


class Lab:
    def __init__(self, ctx, registry, telemetry, ledger):
        self.ctx = ctx
        self.registry = registry
        self.telemetry = telemetry
        self.ledger = ledger
        self.admin_token = ctx.settings.admin_token.get_secret_value()

    def _check_targets(self) -> None:
        hosts = self.ctx.settings.attack_host_set()
        for target in ("switch", "remitter"):
            url = self.ctx.client.base_urls.get(target)
            if url and target not in self.ctx.client.overrides:
                assert_isolated_target(url, hosts)

    def _switch_admin(self, path: str) -> None:
        self.ctx.client.post(
            "switch",
            path,
            {},
            as_identity="CONTROLLER",
            headers={"X-AORTS-Admin-Token": self.admin_token},
        )

    def prepare_pool(
        self, banks=("A1", "A2", "A3"), per_bank: int = 5, destination: str = "B"
    ) -> dict:
        """Create real synthetic payments (response 'lost') so status checks have real targets."""
        pool: dict[str, list[tuple[str, str]]] = {}
        for bank in banks:
            pool[bank] = []
            for i in range(per_bank):
                body = {
                    "sender_account_id": f"{bank}-CUST-{(i % 10) + 1:04d}",
                    "beneficiary_account_id": f"{destination}-CUST-{(i % 10) + 1:04d}",
                    "beneficiary_bank_id": destination,
                    "amount_paise": 1_000 + i,
                    "idempotency_key": f"pool-{uuid.uuid4().hex}",
                }
                r = self.ctx.client.post(
                    "remitter",
                    "/api/v1/payments",
                    body,
                    as_identity="CHANNEL",
                    headers={
                        "X-AORTS-Simulate": "lost-response",
                        "X-AORTS-Admin-Token": self.admin_token,
                    },
                )
                r.raise_for_status()
                t = r.json()["transaction"]
                pool[bank].append((t["transaction_id"], t["route_id"]))
        return pool

    # ------------------------------------------------------------------ Attack 1
    def flooding(self, req: AttackRunRequest, background: bool = True) -> str:
        self._check_targets()
        arms = [req.defenses_enabled] if req.defenses_enabled is not None else [False, True]
        dest = req.target_route.split("-", 1)[1]

        def _run(exp_id: str) -> dict:
            pool = self.prepare_pool(destination=dest)
            out = {"arms": {}}
            for enabled in arms:
                self.ctx.runtime.set_defenses(
                    DefenseConfig(rate_limiting=enabled, tier2_credits=enabled), "LAB"
                )
                time.sleep(1.2)  # let every service's runtime-config cache refresh
                self._switch_admin("/api/v1/admin/security/reset")
                cfg = FloodConfig(
                    attack_rps=req.rate_per_second,
                    duration_s=req.duration_seconds,
                    destination=dest,
                    seed=req.seed,
                )
                result = run_flood(self.ctx.client, pool, cfg)
                out["arms"]["defenses_on" if enabled else "defenses_off"] = {
                    "summary": result.summary(bank_limit=self.ctx.settings.bank_check_rate_per_sec),
                    "requests": [r.__dict__ for r in result.records],
                }
            self.ctx.runtime.set_defenses(DefenseConfig(), "LAB")  # always restore defenses
            return out

        return self.registry.start(
            "flooding",
            _run,
            config=req.model_dump(),
            seed=req.seed,
            experiment_id=req.experiment_id,
            background=background,
        )

    # ------------------------------------------------------------------ Attack 2
    def poisoning(self, req: AttackRunRequest, background: bool = True) -> str:
        def _run(exp_id: str) -> dict:
            model_runs = {}
            for case in CASES:
                for arm in ("clean", "poisoned_unprotected", "poisoned_protected"):
                    r = run_poisoning(PoisonConfig(case=case, arm=arm, seed=req.seed))
                    model_runs[f"{case}/{arm}"] = {"summary": r["summary"], "rows": r["rows"]}
            live = self._live_poison(req.target_route, exp_id)
            return {
                "model_in_the_loop": model_runs,
                "live_ingestion": live,
                "note": "model_in_the_loop uses a fluid-queue route model as ground truth (simulation)",
            }

        return self.registry.start(
            "poisoning",
            _run,
            config=req.model_dump(),
            seed=req.seed,
            experiment_id=req.experiment_id,
            background=background,
        )

    def _live_poison(self, route: str, exp_id: str) -> list[dict]:
        """Send poisoned samples through the real ingestion path (compromised probe twin-probe-2)."""
        out = []
        seq = int(time.time() * 1000)
        samples = [
            (
                "throughput_high",
                dict(
                    throughput_tps=900.0,
                    latency_p50_ms=5.0,
                    latency_p95_ms=8.0,
                    timeout_rate=0.0,
                    backlog=0,
                ),
                "twin-probe-2",
            ),
            ("impossible_range", dict(latency_p50_ms=-1.0, throughput_tps=10.0), "twin-probe-2"),
            ("unauthenticated", dict(throughput_tps=50.0), None),
            ("spoofed_source", dict(throughput_tps=50.0), "twin-probe-1"),
        ]
        for name, values, signer in samples:
            seq += 1
            obs = RouteTelemetry(
                event_id=new_id("tel"),
                source_id="twin-probe-2",
                route_id=route,
                sequence=seq,
                timestamp=datetime.now(UTC),
                correlation_id=exp_id,
                **values,
            )
            a = self.telemetry.ingest(obs, authenticated_source=signer, experiment_id=exp_id)
            out.append({"sample": name, **a.as_dict()})
        dup = out and RouteTelemetry(
            event_id=out[0]["event_id"],
            source_id="twin-probe-2",
            route_id=route,
            sequence=seq + 1,
            timestamp=datetime.now(UTC),
            throughput_tps=10.0,
        )
        if dup:
            a = self.telemetry.ingest(
                dup, authenticated_source="twin-probe-2", experiment_id=exp_id
            )
            out.append({"sample": "duplicate_event_id", **a.as_dict()})
        return out

    # ------------------------------------------------------------------ Attack 3
    def replay(self, req: AttackRunRequest, background: bool = True) -> str:
        self._check_targets()

        def _run(exp_id: str) -> dict:
            sim = ReplaySimulator(self.ctx.client, self.ctx.db, self.admin_token)
            records = sim.run_all()
            return {"cases": records, "ledger_verification": self.ledger.verify_ledger()}

        return self.registry.start(
            "replay",
            _run,
            config=req.model_dump(),
            seed=req.seed,
            experiment_id=req.experiment_id,
            background=background,
        )
