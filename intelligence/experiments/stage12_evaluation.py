"""Stage 12: reproducible evaluation of AORTS against the Stage 05 baselines, with ablations.

Design (frozen in ``DESIGN`` before the final runs; its digest is in every manifest line):

* Workloads: one immutable Stage 3A workload per (scenario, seed); every arm of a seed sees the
  identical exogenous workload (paired design).
* Engine: the independent SimPy DES for every arm. The Stage 04S selector's own verdict for the
  scenario is recorded per run; the DES is used even where the fluid model is permitted because
  the AORTS arms need the DES's causal remitter observations, and one engine per comparison
  avoids a model confound.
* Arms: the Stage 05 STRICT baselines, AORTS (FULL) and its ablations, plus two weight-profile
  sensitivity arms. Track B's heuristic is NOT comparable here (``baselines.NOT_COMPARABLE``).
* Impairments (stale or poisoned telemetry, controller outage) change only what an AORTS arm
  observes; baselines do not read telemetry, so for them those scenarios equal their base.
* No simulator truth reaches a policy; truth is read only by the harness to compute metrics.

    python -m intelligence.experiments.stage12_evaluation run --workers 8
    python -m intelligence.experiments.stage12_evaluation analyse

Raw run records go to ``results/processed/stage12/runs.jsonl`` (and the manifest beside them);
the compact analysis (CSV, JSON, Markdown, figures) goes to ``docs/research/stage12``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import random
import statistics
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

from intelligence.controller.optimizer import PROFILES, OptimizerConfig
from intelligence.experiments import capacity_evaluation as ce
from intelligence.experiments.aorts_policy import AortsPolicy, Impairment
from intelligence.experiments.baselines import make_policy
from intelligence.experiments.policy_harness import SafetyConfig, arm_selection, run_policy
from intelligence.simulation.model_selection import Model, Purpose
from intelligence.simulation.scenarios import (
    CheckDemandSpec,
    RepaySpec,
    ScenarioOptions,
    reference_scenario,
)

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "results" / "processed" / "stage12"
OUT = ROOT / "docs" / "research" / "stage12"
DESIGN_VERSION = "stage12-design/1"
SCALE = 0.02  # 1/50 of the report's reference rates (load ratios preserved)
DURATION_S = 1500.0
BASELINES = ("no-check", "static-polling", "exp-backoff-jitter", "fixed-budget", "aimd")
AORTS = ("aorts",)
ABLATIONS = (
    "aorts-no-feedback",
    "aorts-no-lookahead",
    "aorts-no-uncertainty",
    "aorts-no-deadline",
    "aorts-fixed",
)
SENSITIVITY = ("aorts@ambiguity_averse", "aorts@recovery_seeking")
# Key comparisons (30 paired seeds, every arm); the rest: 10 seeds, baselines + AORTS.
KEY = ("lost_confirmation", "prolonged_fault", "repay_feedback", "sudden_degradation")
KEY_SEEDS = tuple(range(12001, 12031))
OTHER_SEEDS = tuple(range(12001, 12011))


def _one(rho: float = 0.7, share: float = 1.0):
    return (("A1-B", "B", rho, share),)


def _custom(name, seed, routes, faults=(), **update):
    config = ce._custom(name, seed, routes, faults, scale=SCALE, duration=DURATION_S)
    return config.model_copy(
        update={"experiment_id": "EXP-S12", "scenario_id": f"s12-{name}", **update}
    )


def _ref(name, seed, **options):
    return reference_scenario(
        name,
        seed=seed,
        experiment_id="EXP-S12",
        rate_scale=SCALE,
        duration_s=2400.0,
        options=ScenarioOptions(**options),
    )


def _lost_confirmation(seed):
    return ce.recovery_valuable_design(seed, scale=SCALE, duration=DURATION_S).model_copy(
        update={"experiment_id": "EXP-S12", "scenario_id": "s12-lost_confirmation"}
    )


def _burst_demand():
    capacity = 1000.0 * SCALE
    return (
        CheckDemandSpec(
            demand_id="a3-burst",
            bank_id="A3",
            route_id="A1-B",
            start_s=400.0,
            end_s=700.0,
            rate_rps=0.25 * capacity,
        ),
    )


# name -> (builder(seed) -> ScenarioConfig, impairment or None, declared expectation)
SCENARIOS = {
    "normal_load": (
        lambda s: _custom("normal_load", s, _one(0.7)),
        None,
        "healthy route; ambiguity is rare, so checks have little legitimate benefit",
    ),
    "near_saturation": (
        lambda s: _custom("near_saturation", s, _one(0.95)),
        None,
        "rho 0.95; extra checks consume scarce capacity",
    ),
    "sudden_degradation": (
        lambda s: _custom(
            "sudden_degradation", s, _one(0.7), (ce._cut("A1-B", 595.0, DURATION_S, 0.5),)
        ),
        None,
        "capacity -50 % at 595 s until the end; unannounced drop (estimator detection lag)",
    ),
    "prolonged_fault": (
        lambda s: _ref("S2", s),
        None,
        "report S2: capacity -35 % for 20 minutes; Stage 05 found checks counterproductive",
    ),
    "status_check_bursts": (
        lambda s: _custom(
            "status_check_bursts",
            s,
            _one(0.7),
            (ce._cut("A1-B", 100.0, DURATION_S, 0.3, "RESPONSE_LOSS"),),
            check_demand=_burst_demand(),
        ),
        None,
        "30 % lost confirmations (checks can resolve) plus exogenous A3 check demand at 25 % of "
        "capacity for 300 s on the shared route",
    ),
    "repay_feedback": (
        lambda s: _custom(
            "repay_feedback",
            s,
            _one(0.7),
            (ce._cut("A1-B", 400.0, DURATION_S, 0.35),),
            repay=RepaySpec(probability=1.0),
        ),
        None,
        "every customer re-pays an ambiguous payment; capacity -35 % from 400 s",
    ),
    "lost_confirmation": (
        _lost_confirmation,
        None,
        "RV-1: 30 % of confirmations lost and capacity -20 % (300-1200 s); checks can resolve",
    ),
    "route_recovery": (
        lambda s: _custom("route_recovery", s, _one(0.7), (ce._cut("A1-B", 300.0, 600.0, 0.35),)),
        None,
        "capacity -35 % for 300 s, then a recovered route",
    ),
    "healthy_parallel_route": (
        lambda s: _ref("S2", s, healthy_parallel_route=True),
        None,
        "S2 with a healthy A1-C beside the degraded A1-B (route isolation and fairness)",
    ),
    "stale_telemetry": (
        _lost_confirmation,
        Impairment(silent=(500.0, 800.0)),
        "RV-1 with no telemetry delivered to AORTS for 300 s",
    ),
    "poisoned_telemetry": (
        lambda s: _ref("S2", s),
        Impairment(inflate_from_s=600.0, inflate_until_s=1800.0, inflate_factor=2.0),
        "S2 with AORTS's own-traffic throughput reported x2 during the fault (unflagged)",
    ),
    "poisoned_telemetry_30": (
        lambda s: _ref("S2", s),
        Impairment(inflate_from_s=600.0, inflate_until_s=1800.0, inflate_factor=1.3),
        "S2 with throughput reported x1.3 during the fault (unflagged)",
    ),
    "controller_outage": (
        _lost_confirmation,
        Impairment(outage=(500.0, 800.0)),
        "RV-1 with the controller unavailable for 300 s (Track B fallback, 2/route/decision)",
    ),
}


def arms_for(scenario: str) -> tuple[str, ...]:
    if scenario in KEY:
        return BASELINES + AORTS + ABLATIONS + SENSITIVITY
    return BASELINES + AORTS


def seeds_for(scenario: str) -> tuple[int, ...]:
    return KEY_SEEDS if scenario in KEY else OTHER_SEEDS


DESIGN = {
    "version": DESIGN_VERSION,
    "scale": SCALE,
    "duration_s": DURATION_S,
    "engine": "DES for every arm",
    "safety": asdict(SafetyConfig()),
    "scenarios": {k: v[2] for k, v in SCENARIOS.items()},
    "impairments": {k: asdict(v[1]) if v[1] else None for k, v in SCENARIOS.items()},
    "arms": {k: list(arms_for(k)) for k in SCENARIOS},
    "seeds": {k: list(seeds_for(k)) for k in SCENARIOS},
    "optimizer_default": OptimizerConfig().model_dump(mode="json"),
}


def design_digest() -> str:
    return hashlib.sha256(json.dumps(DESIGN, sort_keys=True, default=str).encode()).hexdigest()


def source_digest() -> str:
    """SHA-256 over every Python file of Track A and the contracts (the code that runs)."""

    h = hashlib.sha256()
    for base in ("intelligence", "contracts"):
        for p in sorted((ROOT / base).rglob("*.py")):
            h.update(p.relative_to(ROOT).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def build_policy(arm: str, scenario_config, impairment):
    if not arm.startswith("aorts"):
        return make_policy(arm)
    base, _, profile = arm.partition("@")
    optimizer = OptimizerConfig(profile=PROFILES[profile]) if profile else None
    return AortsPolicy(scenario_config, base, label=arm, optimizer=optimizer, impairment=impairment)


def run_one(scenario: str, arm: str, seed: int) -> dict:
    builder, impairment, _ = SCENARIOS[scenario]
    config = builder(seed)
    policy = build_policy(arm, config, impairment)
    selection = arm_selection(config, policy, Purpose.PRIMARY, SafetyConfig())
    began = time.perf_counter()
    record = run_policy(
        config,
        arm,
        Purpose.PRIMARY,
        engine=Model.DES,
        engine_decision="stage12: DES for every arm (AORTS needs causal remitter observations)",
        policy=policy,
    )
    return {
        "scenario": scenario,
        "arm": arm,
        "seed": seed,
        "scenario_id": config.scenario_id,
        "selector_model": selection.model.value,
        "selector_status": selection.status.value,
        "runtime_s": round(time.perf_counter() - began, 3),
        "record": json.loads(record.to_json()),
        "policy_state": json.loads(json.dumps(dict(policy.state()), default=str)),
    }


def _key(r: dict) -> tuple:
    return (r["scenario"], r["arm"], r["seed"])


def cmd_run(args) -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    runs_path, manifest_path = RAW / "runs.jsonl", RAW / "manifest.jsonl"
    src, design = source_digest(), design_digest()
    done = set()
    if runs_path.exists():
        for line in runs_path.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if r["source_sha256"] == src and r["design_sha256"] == design:
                done.add(_key(r))
    plan = [
        (s, a, seed)
        for s in (args.scenarios or SCENARIOS)
        for seed in seeds_for(s)
        for a in arms_for(s)
        if (s, a, seed) not in done
    ]
    env = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "source_sha256": src,
        "design_sha256": design,
        "design": DESIGN,
        "planned": len(plan),
        "already_done": len(done),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (RAW / f"environment-{int(time.time())}.json").write_text(json.dumps(env, indent=2))
    print(f"{len(plan)} runs planned, {len(done)} already done", flush=True)
    failed = 0
    with (
        ProcessPoolExecutor(max_workers=args.workers) as pool,
        runs_path.open("a", encoding="utf-8") as runs,
        manifest_path.open("a", encoding="utf-8") as manifest,
    ):
        futures = {pool.submit(run_one, *job): job for job in plan}
        for i, fut in enumerate(as_completed(futures), 1):
            s, a, seed = futures[fut]
            entry = {"scenario": s, "arm": a, "seed": seed, "source_sha256": src,
                     "design_sha256": design}  # fmt: skip
            try:
                r = fut.result()
                line = json.dumps({**r, "source_sha256": src, "design_sha256": design},
                                  sort_keys=True)  # fmt: skip
                runs.write(line + "\n")
                runs.flush()
                entry |= {"status": "completed", "runtime_s": r["runtime_s"],
                          "result_hash": r["record"]["result_hash"],
                          "line_sha256": hashlib.sha256(line.encode()).hexdigest()}  # fmt: skip
            except Exception as exc:  # noqa: BLE001 - a failed run is recorded, not hidden
                failed += 1
                entry |= {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:500]}
            manifest.write(json.dumps(entry, sort_keys=True) + "\n")
            manifest.flush()
            if i % 50 == 0 or i == len(plan):
                print(f"{i}/{len(plan)} done, {failed} failed", flush=True)


# --------------------------------------------------------------------------- analysis

METRICS = {
    # name: (getter, higher is better?)
    "new_ambiguous": (lambda m: m["new_ambiguous"], False),
    "resolved_by_check": (lambda m: m["resolved_by_check"], True),
    "unresolved_at_end": (lambda m: m["unresolved_at_end"], False),
    "checks_released": (lambda m: m["checks_released"], None),
    "payment_latency_p95_s": (lambda m: m["payment_latency_p95_s"], False),
    "payment_latency_p99_s": (lambda m: m["payment_latency_p99_s"], False),
    "deadline_completion": (lambda m: m["deadline_completion"], True),
    "deadline_censored": (lambda m: m["deadline_censored"], None),
    "recovery_goodput": (lambda m: m["recovery_goodput"], True),
    "time_to_recovery_s": (lambda m: m["time_to_recovery_s"], False),
    "fairness_jain": (lambda m: m["fairness_jain"], True),
    "safety_violations": (
        lambda m: m["invalid_releases"] + m["noncompliant_releases"],
        False,
    ),
}


def bootstrap_ci(diffs: list[float], reps: int = 10000, seed: int = 12) -> tuple[float, float]:
    rng = random.Random(seed)  # noqa: S311 - research resampling, fixed seed
    n = len(diffs)
    means = sorted(sum(rng.choice(diffs) for _ in range(n)) / n for _ in range(reps))
    return means[int(0.025 * reps)], means[int(0.975 * reps) - 1]


def paired(rows: dict, scenario: str, arm: str, ref: str, metric: str) -> dict | None:
    get = METRICS[metric][0]
    diffs = []
    for seed in seeds_for(scenario):
        a, b = rows.get((scenario, arm, seed)), rows.get((scenario, ref, seed))
        if a is None or b is None:
            continue
        x, y = get(a), get(b)
        if x is None or y is None:
            continue
        diffs.append(float(x) - float(y))
    if not diffs:
        return None
    n = len(diffs)
    out = {
        "n": n,
        "mean": statistics.fmean(diffs),
        "median": statistics.median(diffs),
        "sd": statistics.stdev(diffs) if n > 1 else None,
        "wins": sum(d < 0 for d in diffs) if METRICS[metric][1] is False else None,
        "positive": sum(d > 0 for d in diffs),
        "negative": sum(d < 0 for d in diffs),
        "zero": sum(d == 0 for d in diffs),
    }
    if n >= 10 and len(set(diffs)) > 1:
        out["ci95_bootstrap"] = bootstrap_ci(diffs)
    return out


def load_rows(src: str | None = None):
    rows, meta = {}, defaultdict(set)
    for line in (RAW / "runs.jsonl").read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        meta["source"].add(r["source_sha256"])
        meta["design"].add(r["design_sha256"])
        if src and r["source_sha256"] != src:
            continue
        rows[_key(r)] = r["record"]["metrics"] | {
            "_state": r["policy_state"],
            "_selector": r["selector_model"],
        }
    return rows, meta


def cmd_analyse(args) -> None:
    from intelligence.experiments import stage12_report

    stage12_report.analyse(args)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description="Stage 12 AORTS evaluation")
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run")
    r.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    r.add_argument("--scenarios", nargs="+", choices=list(SCENARIOS))
    a = sub.add_parser("analyse")
    a.add_argument("--out", default=str(OUT))
    a.add_argument("--source", default=None, help="source digest of the runs to analyse")
    args = p.parse_args(argv)
    {"run": cmd_run, "analyse": cmd_analyse}[args.command](args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
