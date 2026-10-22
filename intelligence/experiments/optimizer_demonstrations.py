"""Executed Stage 08 demonstrations: real twins, estimator, look-ahead and optimizer decisions.

Each DES demonstration builds the controller's observable state at a decision instant from a
DES prefix (Stage 07 protocol: own-traffic telemetry -> Route Twin -> Stage 06 estimator;
transaction facts from remitter observations), runs ``decide`` and then measures, with matched
DES continuations, what the chosen budget, the zero budget and a fixed reference budget
actually did over the next 60 s. Truth is read only in that outcome step. These are
development demonstrations, not held-out evidence, and nothing is forced: the decision is
whatever the optimizer chose from the evidence.
"""

import hashlib
import json
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from contracts.v1_1 import SCHEMA_VERSION, ValidatedRouteTelemetry
from intelligence.common.clock import ManualClock
from intelligence.controller.lookahead import CheckCandidate, CheckOutcomeCounts
from intelligence.controller.optimizer import (
    Ablation,
    DecisionContext,
    OptimizationResult,
    OptimizerConfig,
    RouteObservables,
    decide,
)
from intelligence.controller.ranker import PoolEntry
from intelligence.data.generate_dataset import generate_workload
from intelligence.experiments import capacity_evaluation as ce
from intelligence.experiments import lookahead_evaluation as le
from intelligence.ml.capacity_estimator import CapacityEstimator
from intelligence.simulation.des_validate import DesConfig, simulate_des
from intelligence.twin.route_twin import RouteTwinRegistry

SEED = 7101
FIXED_REFERENCE_RPS = 3


def _twin_state(case: le.LookaheadCase, workload, inflate_from_s: float | None = None):
    """Route Twin snapshot and Stage 06 estimate at t0 from the prefix (observable data)."""

    config = case.config
    t0 = case.decision_s
    prefix = simulate_des(workload, DesConfig(bin_s=1.0, horizon_s=t0), le.probe_schedule(case))
    telemetry = ce.des_telemetry(
        ce.remitter_view(prefix),
        experiment_id=config.experiment_id,
        routes=[r.route_id for r in config.routes],
        epoch=config.epoch,
        horizon_s=t0,
        client_timeout_s=config.timing_policy.client_timeout_s,
    )[case.route_id]
    clock = ManualClock(config.epoch)
    twin = RouteTwinRegistry(ce.twin_config(), clock)
    twin.register_route(
        config.route(case.route_id).descriptor(config.experiment_id),
        [f"sim-remitter-{case.route_id}"],
    )
    estimator = CapacityEstimator(config.experiment_id, case.route_id)
    estimate = None
    for obs in telemetry:
        end = obs.observation_window_end
        clock.set_time(end)
        t_end = (end - config.epoch).total_seconds()
        frozen = case.freeze is not None and case.freeze[0] <= t_end - 1.0 < case.freeze[1]
        if inflate_from_s is not None and t_end > inflate_from_s:
            obs = ce.inflate_throughput(obs, 2.0)
        if not frozen:
            twin.ingest(
                ValidatedRouteTelemetry(
                    schema_version=SCHEMA_VERSION,
                    experiment_id=obs.experiment_id,
                    route_id=obs.route_id,
                    telemetry=obs,
                    trust_status="AUTHENTICATED",
                    validator_id="eval-validator",
                    validator_version="eval-1",
                    validated_at=obs.observation_timestamp,
                    cross_check_status="UNAVAILABLE",
                )
            )
        estimate = estimator.update(twin.snapshot(config.experiment_id, case.route_id), end)
    return twin.snapshot(config.experiment_id, case.route_id), estimate


def context_for(case: le.LookaheadCase, workload, inflate_from_s=None) -> DecisionContext:
    snapshot, estimate = _twin_state(case, workload, inflate_from_s)
    fi = le.observe(case, workload).forecast_input
    policy = case.config.timing_policy
    pool = tuple(
        PoolEntry(
            candidate=CheckCandidate(**{**asdict(c), "remitter_bank_id": "A1"}),
            ambiguity_created_at=(
                c.deadline_at - timedelta(seconds=3600.0 - policy.client_timeout_s)
                if c.deadline_at
                else None
            ),
            deadline_escalated=False,
        )
        for c in fi.candidates
    )
    return DecisionContext(
        experiment_id=case.config.experiment_id,
        route_id=case.route_id,
        decision_time=fi.decision_time,
        snapshot=snapshot,
        capacity=estimate,
        pool=pool,
        observables=RouteObservables(
            observed_outstanding=fi.observed_outstanding,
            observed_median_latency_s=fi.observed_median_latency_s,
            observed_timeout_rate=fi.observed_timeout_rate,
            late_resolution_hazard_per_s=fi.late_resolution_hazard_per_s,
            ambiguous_backlog=fi.ambiguous_backlog,
            current_check_rps=fi.current_check_rps,
            check_outcomes=fi.check_outcomes,
        ),
        policy=policy,
    )


def _summary(result: OptimizationResult) -> dict:
    d = result.decision
    return {
        "ablation": result.ablation.value,
        "mode": d.decision_mode.value,
        "reason": d.reason_code.value,
        "budget_rps": d.budget_rps,
        "selected": len(d.selected_transaction_ids),
        "cap_rps": result.cap_rps,
        "predicted_new_ambiguous": d.predicted_new_ambiguous,
        "predicted_delay_ms": d.predicted_delay_ms,
        "detail": d.reason_detail,
        "decision_id": d.decision_id,
    }


def _outcome(case, workload, budget):
    a = le.actual_outcome(case, workload, budget)
    return {
        "budget_rps": budget,
        "new_ambiguous": a.new_ambiguous,
        "resolved_by_check": a.resolved_by_check,
        "checks_released": a.checks_released,
        "queue_mean": a.queue_mean,
        "backlog_end": a.backlog_end,
    }


def des_demo(name: str, case: le.LookaheadCase, inflate_from_s=None) -> dict:
    workload = generate_workload(case.config)
    ctx = context_for(case, workload, inflate_from_s)
    research = OptimizerConfig(
        ablation=Ablation.NO_UNCERTAINTY_BOUND, allow_research_ablations=True
    )
    full = decide(ctx)
    nub = decide(ctx, research)
    budgets = sorted({0, full.decision.budget_rps, nub.decision.budget_rps, FIXED_REFERENCE_RPS})
    return {
        "demo": name,
        "case": case.name,
        "route_id": case.route_id,
        "decision_s": case.decision_s,
        "estimator": {
            "mode": ctx.capacity.mode.value,
            "route_state": ctx.capacity.route_state.value,
            "identifiability": ctx.capacity.identifiability.value,
            "lower_bound_rps": ctx.capacity.capacity_lower_bound_rps,
            "safe_headroom_rps": ctx.capacity.safe_headroom_rps,
            "reasons": [r.value for r in ctx.capacity.reason_codes],
        },
        "eligible_now": sum(1 for e in ctx.pool if e.candidate.eligible_at <= ctx.decision_time),
        "check_outcomes": asdict(ctx.observables.check_outcomes),
        "full": _summary(full),
        "no_uncertainty_bound_research": _summary(nub),
        "actual_next_60s": [_outcome(case, workload, b) for b in budgets],
    }


def _synthetic_route(phases):
    """A Route Twin + Stage 06 estimator fed synthetic own-traffic windows (rate, queue)."""

    from contracts.v1_1 import RouteDescriptor, RouteTelemetry

    t0 = datetime(2026, 4, 1, 9, 0, tzinfo=UTC)
    clock = ManualClock(t0)
    twin = RouteTwinRegistry(ce.twin_config(), clock)
    twin.register_route(
        RouteDescriptor(
            schema_version=SCHEMA_VERSION,
            experiment_id="EXP-CAP",
            route_id="A1-B",
            remitter_bank_id="A1",
            switch_id="SW",
            beneficiary_bank_id="B",
        ),
        ["src-A1-B"],
    )
    estimator = CapacityEstimator("EXP-CAP", "A1-B")
    k, estimate = 0, None
    for windows, rate, queue in phases:
        for _ in range(windows):
            start, end = t0 + timedelta(seconds=k), t0 + timedelta(seconds=k + 1)
            n = int(rate)
            obs = RouteTelemetry(
                schema_version=SCHEMA_VERSION,
                experiment_id="EXP-CAP",
                route_id="A1-B",
                source_id="src-A1-B",
                observation_scope="REMITTER_OWN_TRAFFIC",
                sequence=k,
                observation_window_start=start,
                observation_window_end=end,
                observation_timestamp=end,
                payment_rps=rate,
                check_rps=0.0,
                observed_throughput_rps=rate,
                p50_latency_ms=150.0,
                p95_latency_ms=300.0,
                latency_sample_count=n,
                timeout_rate=0.0,
                timeout_count=0,
                timeout_denominator_count=n,
                queue_depth=queue,
            )
            clock.set_time(end)
            twin.ingest(
                ValidatedRouteTelemetry(
                    schema_version=SCHEMA_VERSION,
                    experiment_id="EXP-CAP",
                    route_id="A1-B",
                    telemetry=obs,
                    trust_status="AUTHENTICATED",
                    validator_id="demo-validator",
                    validator_version="demo-1",
                    validated_at=end,
                    cross_check_status="UNAVAILABLE",
                )
            )
            estimate = estimator.update(twin.snapshot("EXP-CAP", "A1-B"), end)
            k += 1
    return twin.snapshot("EXP-CAP", "A1-B"), estimate, clock.now()


def deadline_demo() -> dict:
    """Synthetic healthy route with headroom; pool with mixed deadlines (Stage 06 feed)."""

    snapshot, estimate, now = _synthetic_route([(120, 30.0, 4), (150, 45.0, 6), (150, 20.0, 3)])
    policy = le.cases(SEED)[0].config.timing_policy

    def entry(i, deadline_s, escalated):
        return PoolEntry(
            CheckCandidate(
                "EXP-CAP",
                "A1-B",
                f"D{i:03d}",
                2,
                now - timedelta(seconds=10),
                0,
                now + timedelta(seconds=deadline_s),
                remitter_bank_id="A1",
            ),
            now - timedelta(seconds=1000 - i),
            escalated,
        )

    pool = tuple(entry(i, 3000 - 10 * i, False) for i in range(30)) + tuple(
        entry(100 + i, 300 + 20 * i, True) for i in range(5)
    )
    ctx = DecisionContext(
        "EXP-CAP",
        "A1-B",
        now,
        snapshot,
        estimate,
        pool,
        RouteObservables(
            observed_outstanding=3,
            observed_median_latency_s=0.3,
            observed_timeout_rate=0.0,
            late_resolution_hazard_per_s=0.0,
            ambiguous_backlog=len(pool),
            check_outcomes=CheckOutcomeCounts(authoritative=30, unknown=3),
        ),
        policy,
    )
    full = decide(ctx)
    age = decide(ctx, OptimizerConfig(ablation=Ablation.NO_DEADLINE_RANKING))
    return {
        "demo": "deadline_prioritization",
        "full": _summary(full) | {"first_selected": full.decision.selected_transaction_ids[:6]},
        "no_deadline_ranking": _summary(age)
        | {"first_selected": age.decision.selected_transaction_ids[:6]},
        "escalated_ids": [e.transaction_id for e in pool if e.deadline_escalated],
    }


def demonstrations() -> list[dict]:
    by_name = {c.name: c for c in le.cases(SEED)}
    rv = by_name["lost_confirmation_recovery"]
    rv_after = le.LookaheadCase(
        "lost_confirmation_after_recovery",
        "RV-1 after the fault ends",
        rv.config,
        "A1-B",
        1320.0,
        rv.budgets,
    )
    s1 = le.LookaheadCase(
        "untrusted_inflation",
        "S1-like route; throughput reported x2",
        by_name["low_load"].config,
        "A1-B",
        600.0,
        (0, 1, 3),
    )
    parallel_b = replace(
        by_name["healthy_parallel_route"], name="parallel_degraded_A1-B", route_id="A1-B"
    )
    return [
        des_demo("congested_route_zero_checks", by_name["prolonged_capacity_loss"]),
        des_demo("recovery_valuable_during_fault", rv),
        des_demo("recovery_valuable_after_recovery", rv_after),
        des_demo("healthy_parallel_route_A1-C", by_name["healthy_parallel_route"]),
        des_demo("degraded_route_A1-B_next_to_healthy", parallel_b),
        des_demo("untrusted_stale_telemetry", by_name["stale_telemetry"]),
        des_demo("untrusted_throughput_inflation", s1, inflate_from_s=450.0),
        deadline_demo(),
    ]


def main() -> None:  # pragma: no cover - evidence generator
    out = Path("results/processed/stage08")
    out.mkdir(parents=True, exist_ok=True)
    began = time.perf_counter()
    demos = demonstrations()
    (out / "demonstrations.json").write_text(json.dumps(demos, indent=2, default=str) + "\n")
    manifest = {
        "label": "Stage 08 executed demonstrations (development, not held-out evidence)",
        "seed": SEED,
        "generated_by": "intelligence.experiments.optimizer_demonstrations",
        "source_digest": _digest(),
        "elapsed_s": round(time.perf_counter() - began, 1),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    files = sorted(p for p in out.iterdir() if p.name != "SHA256SUMS")
    (out / "SHA256SUMS").write_text(
        "".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in files)
    )
    for d in demos:
        f = d["full"]
        print(d["demo"], f["mode"], f["reason"], f["budget_rps"], f["selected"])


def _digest() -> str:
    root = Path(__file__).resolve().parents[2]
    h = hashlib.sha256()
    for rel in (
        "intelligence/controller/optimizer.py",
        "intelligence/controller/ranker.py",
        "intelligence/controller/lookahead.py",
        "intelligence/ml/capacity_estimator.py",
        "intelligence/experiments/optimizer_demonstrations.py",
        "intelligence/experiments/lookahead_evaluation.py",
    ):
        h.update(rel.encode())
        h.update((root / rel).read_bytes())
    return h.hexdigest()


if __name__ == "__main__":  # pragma: no cover
    main()
