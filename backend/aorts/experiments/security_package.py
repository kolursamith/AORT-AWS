"""Track B security validation package: seeded attack experiments with an actual run manifest.

Synthetic, local, isolated environment only (in-process Track B services on the Compose PostgreSQL).
Nothing here contacts a real bank, NPCI/UPI or any public system.

Every run is written to ``<out>/manifest.jsonl`` twice: a ``start`` line before it runs and an
``end`` line (status ``completed`` or ``failed`` with the error) afterwards, so a crashed or failed
run keeps its record. Raw per-run data goes to ``<out>/<experiment>/raw/``; aggregates (mean, std,
t-based 95 % CI; paired bootstrap CI for defended vs undefended) to ``<out>/<experiment>/``.

    python -m backend.aorts.experiments.security_package environment
    python -m backend.aorts.experiments.security_package calibrate
    python -m backend.aorts.experiments.security_package flooding --seeds 101 102 103 104 105
    python -m backend.aorts.experiments.security_package poisoning --seeds 101 102 103 104 105
    python -m backend.aorts.experiments.security_package poisoning-live --seeds 101 102 103 104 105
    python -m backend.aorts.experiments.security_package replay --seeds 101 102 103 104 105
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import platform
import statistics
import subprocess
import threading
import time
import traceback
import uuid
from collections import defaultdict
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from sqlalchemy import text

from backend.aorts.app.config import Settings
from backend.aorts.app.metrics import percentile
from backend.aorts.contracts.enums import TelemetryVerdict
from backend.aorts.contracts.requests import DefenseConfig, RouteTelemetry
from backend.aorts.control.traffic import TrafficConfig, TrafficGenerator
from backend.aorts.experiments.common import ROOT, inprocess_stack, stats
from backend.aorts.experiments.paired_stats import bootstrap_mean_ci
from backend.aorts.experiments.provenance import source_sha256
from backend.aorts.experiments.run_flooding import build_pool
from backend.aorts.security.flooding_simulator import FloodConfig, run_flood
from backend.aorts.security.poisoning_simulator import (
    ARMS,
    EXTENDED_CASES,
    PoisonConfig,
    run_poisoning,
)
from backend.aorts.security.replay_simulator import ReplaySimulator
from backend.aorts.security.telemetry_validator import TelemetryValidator

OUT = ROOT / "infra" / "trackb" / "evidence" / "security"
DEFAULT_SEEDS = [101, 102, 103, 104, 105]
# Locally calibrated switch capacity for the flooding runs (documented in every manifest line): a
# smaller, slower pool than the defaults so that a flood the in-process load generator can really
# deliver is able to saturate it. See calibration.json for the measured ramp.
FLOOD_CAPACITY = {
    "switch_core_workers": 8,
    "switch_base_processing_ms": 40,
    "switch_check_workers": 0,
}
TIMELINE = {"lead_s": 4.0, "attack_s": 8.0, "tail_s": 6.0}
LEGIT_RPS_PER_BANK = 3.0
PAYMENT_RPS = 4.0
FLOOD_CONDITIONS = {
    # condition: (attack, defenses on, destination of A1/A2 traffic)
    "normal": (False, True, "B"),
    "flood_undefended": (True, False, "B"),
    "flood_defended": (True, True, "B"),
    "parallel_route_undefended": (True, False, "C"),
    "parallel_route_defended": (True, True, "C"),
}
UNRESOLVED_STATES = ("DEBITED", "AMBIGUOUS", "CHECK_IN_FLIGHT")


# ---------------------------------------------------------------- provenance and manifest
def _now() -> str:
    return datetime.now(UTC).isoformat()


def _ver(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def _docker(fmt: str, container: str = "aorts-postgres") -> str | None:
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell, read-only
            ["docker", "inspect", container, "--format", fmt],  # noqa: S607 - read-only
            capture_output=True,
            text=True,
            timeout=15,
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell, read-only
            ["git", *args],  # noqa: S607 - git from PATH
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
        )
        return out.stdout.strip() if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def git_state() -> dict:
    """Commit of the code that ran; untracked evidence files written by runs do not count as dirty."""
    return {
        "git_commit": _git("rev-parse", "HEAD") or "not-a-git-repository",
        "git_branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "tracked_changes_uncommitted": bool(_git("status", "--porcelain", "--untracked-files=no")),
    }


def environment() -> dict:
    s = Settings()
    from backend.aorts.app.db import Database

    db = Database(s.database_url.get_secret_value(), pool_size=1)
    try:
        with db.session() as session:
            pg = session.execute(text("SELECT version()")).scalar()
    finally:
        db.dispose()
    return {
        "recorded_at": _now(),
        **git_state(),
        "source_sha256": source_sha256(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "dependencies": {
            n: _ver(n)
            for n in (
                "fastapi",
                "starlette",
                "pydantic",
                "sqlalchemy",
                "psycopg",
                "alembic",
                "httpx",
                "uvicorn",
            )
        },
        "postgres_server": pg,
        "postgres_container_image": _docker("{{.Config.Image}}"),
        "postgres_image_digest": _docker("{{.Image}}"),
        "label": "synthetic local environment; in-process Track B services; Compose PostgreSQL",
    }


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Manifest:
    def __init__(self, out: Path, experiment: str, env: dict):
        self.out = out
        self.path = out / "manifest.jsonl"
        self.experiment_id = f"secpkg-{experiment}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
        self.experiment = experiment
        self.env = env
        out.mkdir(parents=True, exist_ok=True)

    def _write(self, rec: dict) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")

    def run(self, scenario: str, condition: str, seed: int, config: dict, fn):
        """Run ``fn()`` -> (outputs dict of paths, row dict); record start/end lines; never raise."""
        run_id = f"{self.experiment_id}-{condition}-s{seed}-{uuid.uuid4().hex[:6]}"
        base = {
            "experiment_id": self.experiment_id,
            "experiment": self.experiment,
            "scenario_id": scenario,
            "condition": condition,
            "seed": seed,
            "run_id": run_id,
            "git_commit": self.env["git_commit"],
            "tracked_changes_uncommitted": self.env["tracked_changes_uncommitted"],
            "source_sha256": self.env["source_sha256"],
            "config": config,
        }
        started = _now()
        self._write({**base, "event": "start", "started_at": started})
        t0 = time.perf_counter()
        try:
            outputs, row = fn(run_id)
            status, error = "completed", None
        except Exception as exc:  # noqa: BLE001 - a failed run is recorded, not hidden
            outputs, row = {}, None
            status, error = "failed", f"{type(exc).__name__}: {exc}"[:500]
            print(f"  FAILED {run_id}: {error}\n{traceback.format_exc()[-1500:]}", flush=True)
        self._write(
            {
                **base,
                "event": "end",
                "started_at": started,
                "ended_at": _now(),
                "duration_s": round(time.perf_counter() - t0, 2),
                "status": status,
                "error": error,
                "outputs": {
                    k: {"path": _rel(v), "sha256": sha256_file(v)} for k, v in outputs.items()
                },
            }
        )
        return status, row


def _rel(path) -> str:
    """Repository-relative path when the file is inside the repository, absolute otherwise."""
    p = Path(path).resolve()
    try:
        return p.relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return p.as_posix()


def write_csv(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    keys: list[str] = []
    for r in rows:
        keys += [k for k in r if k not in keys]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)
    return path


def write_json(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    return path


def aggregate(rows: list[dict], group: str, metrics: list[str]) -> dict:
    by: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by[r[group]].append(r)
    return {
        g: {m: stats([r.get(m) for r in rs if isinstance(r.get(m), (int, float))]) for m in metrics}
        for g, rs in by.items()
    }


def paired_diff(rows: list[dict], cond_a: str, cond_b: str, metric: str) -> dict:
    a = {r["seed"]: r.get(metric) for r in rows if r["condition"] == cond_a}
    b = {r["seed"]: r.get(metric) for r in rows if r["condition"] == cond_b}
    diffs = [a[s] - b[s] for s in sorted(set(a) & set(b)) if a[s] is not None and b[s] is not None]
    if len(diffs) < 2:
        return {"pairs": len(diffs), "note": "insufficient paired runs"}
    ci = bootstrap_mean_ci(diffs)
    return {
        "pairs": len(diffs),
        "mean_diff": round(statistics.fmean(diffs), 4),
        "ci95_bootstrap": [round(ci[0], 4), round(ci[1], 4)],
        "ci_excludes_zero": ci[0] > 0 or ci[1] < 0,
    }


# ---------------------------------------------------------------- Phase B: flooding
def _p(values, q):
    v = percentile(values, q)
    return None if v is None else round(v, 2)


def _jain(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    if not xs or not any(xs):
        return None
    return round(sum(xs) ** 2 / (len(xs) * sum(x * x for x in xs)), 4)


def flood_once(
    condition: str, seed: int, attack_rps: float, run_id: str | None, out: Path | None
) -> tuple[dict, dict]:
    attack, defended, dest = FLOOD_CONDITIONS[condition]
    lead, dur, tail = TIMELINE["lead_s"], TIMELINE["attack_s"], TIMELINE["tail_s"]
    total = lead + dur + tail
    with inprocess_stack(**FLOOD_CAPACITY) as st:
        st.set_defenses(DefenseConfig(rate_limiting=defended, tier2_credits=defended))
        sw = st.switch_service
        sw.admission.reset()
        sw.ctx.metrics.reset()
        admin = st.settings.admin_token.get_secret_value()
        pool = build_pool(st.client, admin, {"A3": "B", "A1": dest, "A2": dest})
        pool_ids = [t for txns in pool.values() for t, _ in txns]
        traffic = TrafficGenerator(
            st.client,
            TrafficConfig(
                rps=PAYMENT_RPS,
                duration_s=total,
                banks=("A1", "A2"),
                destinations=("B", "C"),
                retry_probability=0.0,
                seed=seed,
            ),
        )
        t_res: dict = {}
        th = threading.Thread(target=lambda: t_res.update(traffic.run()))
        th.start()
        result = run_flood(
            st.client,
            pool,
            FloodConfig(
                attack_rps=attack_rps if attack else 0.0,
                duration_s=dur,
                legit_rps_per_bank=LEGIT_RPS_PER_BANK,
                destination="B",
                seed=seed,
                lead_s=lead,
                tail_s=tail,
            ),
        )
        th.join()
        counters = sw.ctx.metrics.snapshot()["counters"]
        service_entries = {
            b: sw.ctx.metrics.latency_stats("check_service_latency", 3600, bank=b)["count"]
            for b in ("A1", "A2", "A3")
        }
        quotas = {b: sw.admission.quota_view(b) for b in ("A1", "A2", "A3")}
        with st.db.session() as s:
            states = dict(
                s.execute(
                    text(
                        "SELECT state, count(*) FROM transactions "
                        "WHERE NOT (transaction_id = ANY(:ids)) GROUP BY state"
                    ),
                    {"ids": pool_ids},
                ).all()
            )
        ledger = st.ledger.verify_ledger()
        bank_limit = st.settings.bank_check_rate_per_sec
    summ = result.summary(bank_limit=bank_limit)
    recs = result.records

    def window(r):
        return "before" if r.t < lead else ("during" if r.t < lead + dur else "after")

    legit = [r for r in recs if r.bank != "A3"]
    row = {
        "condition": condition,
        "seed": seed,
        "attack_rps": attack_rps if attack else 0.0,
        "defenses": "on" if defended else "off",
        "legit_destination": dest,
    }
    for w in ("before", "during", "after"):
        rs = [r for r in legit if window(r) == w]
        ok = [r.latency_ms for r in rs if r.status == 200]
        row[f"legit_sent_{w}"] = len(rs)
        row[f"legit_success_{w}"] = round(len(ok) / len(rs), 4) if rs else None
        row[f"legit_p50_ms_{w}"] = _p(ok, 0.5)
        row[f"legit_p95_ms_{w}"] = _p(ok, 0.95)
    pb = summ["per_bank"]
    for b in ("A1", "A2", "A3"):
        row[f"{b}_sent"] = pb.get(b, {}).get("sent", 0)
        row[f"{b}_admitted"] = pb.get(b, {}).get("admitted_ok", 0)
        row[f"{b}_admitted_ratio"] = pb.get(b, {}).get("admitted_ratio")
        row[f"{b}_server_received"] = counters.get(f"check_received{{bank={b}}}", 0)
        row[f"{b}_server_admitted"] = counters.get(f"check_admitted{{bank={b}}}", 0)
        row[f"{b}_server_rejected"] = sum(
            v for k, v in counters.items() if k.startswith("check_rejected{") and f"bank={b}" in k
        )
        row[f"{b}_entered_processing"] = service_entries[b]
        row[f"{b}_credits_left"] = quotas[b]["credits"]
    for code in (
        "RATE_LIMITED",
        "GLOBAL_CAPACITY_EXCEEDED",
        "ROUTE_RATE_LIMITED",
        "CREDIT_EXHAUSTED",
    ):
        row[f"A3_rejected_{code}"] = counters.get(f"check_rejected{{bank=A3,reason={code}}}", 0)
    a3_rej = [r.latency_ms for r in recs if r.bank == "A3" and r.status == 429]
    a3_ok = [r.latency_ms for r in recs if r.bank == "A3" and r.status == 200]
    row["A3_rejected_p50_ms"] = _p(a3_rej, 0.5)
    row["A3_admitted_p50_ms"] = _p(a3_ok, 0.5)
    # Rejections must happen before the costly path: only admitted checks may enter processing.
    row["rejected_that_entered_processing"] = max(
        0, row["A3_entered_processing"] - row["A3_server_admitted"]
    )
    row["switch_overloaded_checks"] = sum(
        v for k, v in counters.items() if k.startswith("core_overloaded{kind=check")
    )
    row["detection_s"] = summ["attack_detection_s"]
    row["containment_s"] = summ["attack_containment_s"]
    row["recovery_after_attack_s"] = summ.get("recovery_after_attack_s")
    row["baseline_legit_p50_ms"] = summ.get("baseline_legit_ms")
    during = [r for r in recs if lead <= r.t < lead + dur]
    row["fairness_jain_legit_banks"] = _jain(
        [
            (sum(1 for r in during if r.bank == b and r.status == 200) / n)
            if (n := sum(1 for r in during if r.bank == b))
            else None
            for b in ("A1", "A2")
        ]
    )
    admitted_during = [r for r in during if r.status == 200]
    row["attacker_share_of_admitted_during"] = (
        round(sum(1 for r in admitted_during if r.bank == "A3") / len(admitted_during), 4)
        if admitted_during
        else None
    )
    row["payments_sent"] = t_res.get("sent")
    row["payments_closed"] = (t_res.get("outcomes") or {}).get("CLOSED", 0)
    row["payment_p50_ms"] = t_res.get("latency_p50_ms")
    row["payment_p95_ms"] = t_res.get("latency_p95_ms")
    row["payment_throughput_per_s"] = round(row["payments_closed"] / total, 3)
    row["payments_unresolved_at_end"] = sum(states.get(s, 0) for s in UNRESOLVED_STATES)
    row["ledger_ok"] = ledger["ok"]
    raw = {
        "condition": condition,
        "seed": seed,
        "timeline": TIMELINE,
        "capacity": FLOOD_CAPACITY,
        "summary": summ,
        "traffic": t_res,
        "switch_counters": counters,
        "quotas": quotas,
        "transaction_states_excl_attack_pool": states,
        "ledger_verification": ledger,
        "row": row,
    }
    outputs = {}
    if out is not None and run_id is not None:
        outputs["raw_json"] = write_json(out / "raw" / f"{run_id}.json", raw)
        outputs["requests_csv"] = write_csv(
            out / "raw" / f"{run_id}-requests.csv",
            [{**asdict(r), "window": window(r)} for r in sorted(recs, key=lambda r: r.t)],
        )
    return outputs, row


def cmd_calibrate(args) -> None:
    out = Path(args.out)
    ramp = []
    print("calibration ramp (defenses OFF, attacker A3 -> route B, seed 7)", flush=True)
    for rate in args.rates:
        _, row = flood_once("flood_undefended", 7, rate, None, None)
        achieved = row["A3_sent"] / TIMELINE["attack_s"]
        point = {
            "target_rps": rate,
            "achieved_rps": round(achieved, 2),
            "legit_p95_before_ms": row["legit_p95_ms_before"],
            "legit_p95_during_ms": row["legit_p95_ms_during"],
            "legit_success_during": row["legit_success_during"],
            "switch_overloaded_checks": row["switch_overloaded_checks"],
            "payment_p95_ms": row["payment_p95_ms"],
        }
        ramp.append(point)
        print(point, flush=True)
    harmful = [
        p
        for p in ramp
        if p["achieved_rps"] >= 0.9 * p["target_rps"]
        and (
            (p["legit_success_during"] or 1) < 0.95
            or (p["legit_p95_during_ms"] or 0) >= 3 * (p["legit_p95_before_ms"] or 1e9)
        )
    ]
    chosen = (
        harmful[0]["target_rps"]
        if harmful
        else max(
            (p for p in ramp if p["achieved_rps"] >= 0.9 * p["target_rps"]),
            key=lambda p: p["target_rps"],
        )["target_rps"]
    )
    data = {
        "recorded_at": _now(),
        "capacity": FLOOD_CAPACITY,
        "timeline": TIMELINE,
        "rule": "smallest rate the harness delivers (>= 90 % of target) at which undefended legitimate "
        "checks degrade: success < 95 % during the attack or p95 >= 3x the pre-attack p95",
        "ramp": ramp,
        "chosen_attack_rps": chosen,
        "harm_observed_at_chosen_rate": bool(harmful),
    }
    write_json(out / "flooding" / "calibration.json", data)
    print("chosen attack rate:", chosen, flush=True)


def cmd_flooding(args, env) -> None:
    out = Path(args.out)
    cal_path = out / "flooding" / "calibration.json"
    rate = args.attack_rps or json.loads(cal_path.read_text(encoding="utf-8"))["chosen_attack_rps"]
    man = Manifest(out, "flooding", env)
    edir = out / "flooding"
    rows, fails = [], 0
    for seed in args.seeds:
        for cond in args.conditions:
            config = {
                "attack_rps": rate,
                "capacity": FLOOD_CAPACITY,
                "timeline": TIMELINE,
                "legit_rps_per_bank": LEGIT_RPS_PER_BANK,
                "payment_rps": PAYMENT_RPS,
                "defenses": dict(
                    zip(
                        ("attack", "defenses_on", "legit_destination"),
                        FLOOD_CONDITIONS[cond],
                        strict=True,
                    )
                ),
                "admission_limits": {
                    k: getattr(Settings(), k)
                    for k in (
                        "bank_check_rate_per_sec",
                        "bank_check_burst",
                        "route_check_rate_per_sec",
                        "global_check_rate_per_sec",
                        "tier2_credit_cap",
                        "tier2_credit_refill_per_sec",
                    )
                },
            }
            status, row = man.run(
                "B-status-check-flooding",
                cond,
                seed,
                config,
                lambda rid, c=cond, s=seed: flood_once(c, s, rate, rid, edir),
            )
            print(f"flooding {cond} seed={seed}: {status}", flush=True)
            fails = 0 if status == "completed" else fails + 1
            if row:
                rows.append({**row, "experiment_id": man.experiment_id})
            if fails >= 3:
                print("3 consecutive failures: aborting", flush=True)
                break
    write_csv(edir / f"{man.experiment_id}-runs.csv", rows)
    metrics = [k for k in (rows[0] if rows else {}) if isinstance(rows[0][k], (int, float))]
    metrics = [m for m in metrics if m not in ("seed", "attack_rps")]
    paired = {
        f"{a}_minus_{b}": {
            m: paired_diff(rows, a, b, m)
            for m in (
                "legit_success_during",
                "legit_p95_ms_during",
                "A3_server_admitted",
                "payment_p95_ms",
                "payments_unresolved_at_end",
                "attacker_share_of_admitted_during",
            )
        }
        for a, b in (
            ("flood_defended", "flood_undefended"),
            ("parallel_route_defended", "parallel_route_undefended"),
            ("flood_defended", "normal"),
        )
    }
    write_json(
        edir / f"{man.experiment_id}-summary.json",
        {
            "experiment_id": man.experiment_id,
            "attack_rps": rate,
            "seeds": args.seeds,
            "runs_completed": len(rows),
            "by_condition": aggregate(rows, "condition", metrics),
            "paired_bootstrap": paired,
        },
    )


# ---------------------------------------------------------------- Phase C: telemetry poisoning
def cmd_poisoning(args, env) -> None:
    out = Path(args.out)
    man = Manifest(out, "poisoning-model", env)
    edir = out / "poisoning"
    rows = []
    for seed in args.seeds:
        for case in EXTENDED_CASES:
            for arm in ARMS:
                cfg = PoisonConfig(case=case, arm=arm, seed=seed)

                def fn(rid, cfg=cfg):
                    res = run_poisoning(cfg)
                    ticks = [{**t, "reasons": (t["reasons"] or "")[:160]} for t in res["rows"]]
                    return {"ticks_csv": write_csv(edir / "raw" / f"{rid}-ticks.csv", ticks)}, {
                        **res["summary"],
                        "seed": cfg.seed,
                        "condition": f"{cfg.case}/{cfg.arm}",
                    }

                status, row = man.run(
                    "C-telemetry-poisoning-model", f"{case}.{arm}", seed, asdict(cfg), fn
                )
                if row:
                    rows.append({**row, "experiment_id": man.experiment_id})
        print(f"poisoning model seed={seed}: {len(rows)} runs so far", flush=True)
    write_csv(edir / f"{man.experiment_id}-runs.csv", rows)
    metrics = [
        "detection_rate",
        "detection_tick_offset",
        "guard_conservative_ticks",
        "mean_budget_during_attack",
        "max_budget_during_attack",
        "mean_actual_delay_ms",
        "ticks_over_1000ms",
        "flagged_all_ticks",
        "rejected",
        "suspicious",
        "trusted_during_attack",
    ]
    write_json(
        edir / f"{man.experiment_id}-summary.json",
        {
            "experiment_id": man.experiment_id,
            "seeds": args.seeds,
            "note": "Model-in-the-loop: fluid-queue route model is the ground truth; the validator, "
            "twin update rule, mock controller and BudgetGuard are the real Track B components.",
            "false_positives_clean_arm": sum(
                r["flagged_all_ticks"] for r in rows if r["arm"] == "clean"
            ),
            "clean_arm_ticks": sum(r["ticks"] for r in rows if r["arm"] == "clean"),
            "by_case_and_arm": aggregate(rows, "condition", metrics),
        },
    )


LIVE_CASES = {
    # case: (signer, transform(measured) -> reported values or None for "send nothing")
    "honest": "twin-probe-1",
    "throughput_high_4x": "twin-probe-2",
    "latency_low_80pct": "twin-probe-2",
    "timeout_rate_false_zero": "twin-probe-2",
    "backlog_inconsistent": "twin-probe-2",
    "subtle_throughput_30pct": "twin-probe-2",
    "stale_timestamp": "twin-probe-2",
    "wrong_source_identity": "twin-probe-1",  # signed by probe 1, claims probe 2
    "missing": None,
}


def _lie(case: str, m: dict, i: int) -> dict:
    v = {
        "latency_p50_ms": m.get("latency_p50_ms") or 1.0,
        "latency_p95_ms": m.get("latency_p95_ms") or 1.0,
        "throughput_tps": m.get("throughput_tps") or 0.0,
        "timeout_rate": m.get("timeout_rate") or 0.0,
        "backlog": int(m.get("backlog") or 0),
    }
    if case == "throughput_high_4x":
        v["throughput_tps"] = max(v["throughput_tps"] * 4, 40.0)
    elif case == "latency_low_80pct":
        v["latency_p50_ms"] *= 0.2
        v["latency_p95_ms"] *= 0.2
    elif case == "timeout_rate_false_zero":
        v["timeout_rate"] = 0.0
        v["latency_p50_ms"] *= 0.3
        v["latency_p95_ms"] *= 0.3
    elif case == "backlog_inconsistent":
        v["backlog"] = 0 if i % 2 else 400
    elif case == "subtle_throughput_30pct":
        v["throughput_tps"] *= 1.3
    v["latency_p95_ms"] = max(v["latency_p95_ms"], v["latency_p50_ms"])
    return v


def poison_live_once(case: str, arm: str, seed: int, run_id: str, edir: Path):
    """Real ingestion path (test-only lab endpoint) against real switch measurements."""
    protected = arm == "protected"
    samples = []
    with inprocess_stack() as st:
        st.set_defenses(DefenseConfig(telemetry_validation=protected))
        tel = st.control.app.state.telemetry
        s = st.settings
        tel.validator = TelemetryValidator(
            sources=s.telemetry_source_set(),
            max_age_s=s.telemetry_max_age_seconds,
            max_future_s=s.telemetry_max_future_seconds,
            min_confidence=s.telemetry_min_confidence,
            tolerance=s.telemetry_cross_check_tolerance,
            bias_check=s.telemetry_bias_check,
        )
        admin = {"X-AORTS-Admin-Token": s.admin_token.get_secret_value()}
        silence_s = s.telemetry_max_age_seconds + 4
        attack_n = 12
        duration = 6 + 6 * 0.5 + (silence_s if case == "missing" else attack_n * 0.5) + 2
        traffic = TrafficGenerator(
            st.client,
            TrafficConfig(
                rps=PAYMENT_RPS,
                duration_s=duration,
                banks=("A1",),
                destinations=("B",),
                retry_probability=0.0,
                seed=seed,
            ),
        )
        th = threading.Thread(target=traffic.run, daemon=True)
        th.start()
        time.sleep(6)  # the switch needs real measurements before telemetry can be cross-checked
        seq = 1000 + seed * 100

        def measured() -> dict:
            tel._cache = (0.0, {})  # same fresh snapshot the validator is about to use
            return tel._switch_measurements().get("B", {})

        def decide(phase: str, i: int, verdict: dict | None, m: dict) -> None:
            body = {
                "decision_request_id": f"drq-{run_id[-6:]}-{phase}-{i}",
                "timestamp": _now(),
                "routes": [{"route_id": "A1-B", "backlog": 20, "measured": m}],
                "candidates": [],
            }
            r = st.client.post(
                "control", "/api/v1/controller/decisions", body, as_identity="CONTROLLER"
            ).json()
            g = r["guard"]["A1-B"]
            samples.append(
                {
                    "phase": phase,
                    "i": i,
                    "t": _now(),
                    "verdict": (verdict or {}).get("verdict", "NONE_SENT"),
                    "confidence": (verdict or {}).get("confidence"),
                    "reasons": "; ".join((verdict or {}).get("reasons", []))[:200],
                    "measured_throughput_tps": m.get("throughput_tps"),
                    "measured_latency_p50_ms": m.get("latency_p50_ms"),
                    "reported": json.dumps((verdict or {}).get("reported")),
                    "budget_recommended": g["recommended"],
                    "budget_applied": g["applied"],
                    "guard_integrity": g["integrity"],
                    "guard_reasons": "; ".join(g["reasons"])[:200],
                }
            )

        def send(case_name: str, signer: str | None, i: int, phase: str) -> None:
            nonlocal seq
            seq += 1
            m = measured()
            v = _lie(case_name, m, i)
            ts = datetime.now(UTC)
            if case_name == "stale_timestamp":
                ts -= timedelta(seconds=120)
            source_id = "twin-probe-2" if signer else "twin-probe-1"
            obs = RouteTelemetry(
                event_id=f"tel-{run_id[-6:]}-{phase}-{i}",
                source_id=source_id if case_name != "honest" else "twin-probe-1",
                route_id="A1-B",
                sequence=seq,
                timestamp=ts,
                health_score=max(0.0, 1.0 - v["timeout_rate"]),
                **v,
            )
            r = st.control.post(
                "/api/v1/lab/telemetry/inject",
                params={"as_source": signer} if signer else {},
                content=obs.model_dump_json(),
                headers={**admin, "Content-Type": "application/json"},
            )
            r.raise_for_status()
            decide(phase, i, r.json(), m)

        for i in range(6):  # honest phase: the twin learns the real route
            send("honest", "twin-probe-1", i, "honest")
            time.sleep(0.5)
        if case == "missing":
            t_end = time.time() + silence_s
            i = 0
            while time.time() < t_end:
                time.sleep(2.0)
                decide("attack", i, None, measured())
                i += 1
        else:
            for i in range(attack_n):
                send(case, LIVE_CASES[case], i, "attack")
                time.sleep(0.5)
        traffic.stop()
        th.join(timeout=30)
        with st.db.session() as session:
            stored = dict(
                session.execute(
                    text(
                        "SELECT coalesce(experiment_id, 'none'), count(*) FROM telemetry_observations "
                        "GROUP BY 1"
                    )
                ).all()
            )
    attack = [x for x in samples if x["phase"] == "attack"]
    sent = [x for x in attack if x["verdict"] != "NONE_SENT"]
    flagged = [x for x in sent if x["verdict"] != TelemetryVerdict.TRUSTED.value]
    honest_flagged = [
        x
        for x in samples
        if x["phase"] == "honest" and x["verdict"] != TelemetryVerdict.TRUSTED.value
    ]
    row = {
        "condition": f"{case}.{arm}",
        "case": case,
        "arm": arm,
        "seed": seed,
        "attack_samples": len(sent),
        "detection_rate": round(len(flagged) / len(sent), 4) if sent else None,
        "first_flag_index": next(
            (x["i"] for x in attack if x["verdict"] not in ("TRUSTED", "NONE_SENT")), None
        ),
        "honest_phase_false_positives": len(honest_flagged),
        "guard_conservative_during_attack": sum(
            1 for x in attack if x["guard_integrity"] not in ("TRUSTED", "UNCHECKED")
        ),
        "max_budget_applied_attack": max((x["budget_applied"] for x in attack), default=None),
        "max_budget_applied_honest": max(
            (x["budget_applied"] for x in samples if x["phase"] == "honest"), default=None
        ),
        # every injected sample is stored as lab test data; nothing else wrote telemetry
        "injected_samples_stored_as_lab_data": stored.get("lab-inject", 0),
        "telemetry_rows_not_lab_data": sum(v for k, v in stored.items() if k != "lab-inject"),
    }
    return {"samples_csv": write_csv(edir / "raw" / f"{run_id}-samples.csv", samples)}, row


def cmd_poisoning_live(args, env) -> None:
    out = Path(args.out)
    man = Manifest(out, "poisoning-live", env)
    edir = out / "poisoning_live"
    rows = []
    for seed in args.seeds:
        for case in args.cases:
            for arm in ("unprotected", "protected"):
                status, row = man.run(
                    "C-telemetry-poisoning-live",
                    f"{case}.{arm}",
                    seed,
                    {"case": case, "arm": arm, "signer": LIVE_CASES[case]},
                    lambda rid, c=case, a=arm, s=seed: poison_live_once(c, a, s, rid, edir),
                )
                print(f"poisoning live {case}.{arm} seed={seed}: {status}", flush=True)
                if row:
                    rows.append({**row, "experiment_id": man.experiment_id})
    write_csv(edir / f"{man.experiment_id}-runs.csv", rows)
    write_json(
        edir / f"{man.experiment_id}-summary.json",
        {
            "experiment_id": man.experiment_id,
            "seeds": args.seeds,
            "by_case_and_arm": aggregate(
                rows,
                "condition",
                [
                    "detection_rate",
                    "first_flag_index",
                    "honest_phase_false_positives",
                    "guard_conservative_during_attack",
                    "max_budget_applied_attack",
                    "max_budget_applied_honest",
                ],
            ),
        },
    )


# ---------------------------------------------------------------- Phase D: replay
FIRST_USE = {
    "1_valid_response_once",
    "10a_captured_request_first_use",
    "4_same_reversal_delivery_1",
}
EXPECTED = {
    # case prefix -> accepted HTTP statuses (the expectation text in replay_simulator.py is the spec)
    "1_": {200},
    "2_": {200, 409},
    "3_": {409, 422},
    "3b_": {409, 422},
    "4_same_reversal_delivery_1": {200},
    "4_same": {200, 409},
    "4b_": {409, 422},
    "5_": {200, 409},
    "6_": {200, 409},
    "7_": {200, 409},
    "7b_": {409, 422},
    "8_": {409, 422},
    "8b_": {401, 403},
    "9_": {422},
    "9b_": {422},
    "10a_": {200},
    "10b_": {401},
    "10c_": {401},
    "10d_": {401},
    "10e_": {401},
    "11_": {403},
    "11b_": {403},
}


def _expected(case: str) -> set[int]:
    for prefix in sorted(EXPECTED, key=len, reverse=True):
        if case.startswith(prefix):
            return EXPECTED[prefix]
    return set()


def replay_once(seed: int, auto_reverse: bool, run_id: str, edir: Path):
    with inprocess_stack(auto_reverse_on_confirmed_failure=auto_reverse) as st:
        admin = st.settings.admin_token.get_secret_value()
        records = ReplaySimulator(st.client, st.db, admin).run_all()
        st.flush_audit()
        ledger = st.ledger.verify_ledger()
        with st.db.session() as s:
            audit = [
                {"action": a, "decision": d, "count": n}
                for a, d, n in s.execute(
                    text(
                        "SELECT action, decision, count(*) FROM audit_events GROUP BY 1, 2 ORDER BY 1, 2"
                    )
                ).all()
            ]
        chain = st.control.get(
            "/api/v1/audit/verify", headers={"X-AORTS-Admin-Token": admin}
        ).json()
    by_txn: dict[str, dict] = {}
    for r in records:
        exp = _expected(r["case"])
        r["expected_http"] = sorted(exp)
        r["http_as_expected"] = r["http_status"] in exp
        base = by_txn.get(r["transaction_id"])
        fin = (
            r["ledger_postings"],
            r["reversal_postings"],
            r["sender_balance"],
            r["final_outcome"],
        )
        if r["case"] in FIRST_USE or base is None:
            # the first legitimate application (or the first observation) sets the reference state
            r["financial_change_vs_reference"] = None
            by_txn[r["transaction_id"]] = {"fin": fin}
        else:
            r["financial_change_vs_reference"] = fin != base["fin"]
        r["duplicate_reversal_posting"] = (r["reversal_postings"] or 0) > 1
        r.setdefault("status_codes", None)
        r["status_codes"] = str(r["status_codes"]) if r["status_codes"] is not None else None
    row = {
        "condition": f"auto_reverse={auto_reverse}",
        "seed": seed,
        "cases": len(records),
        "http_as_expected": sum(1 for r in records if r["http_as_expected"]),
        "unexpected_financial_changes": sum(
            1 for r in records if r["financial_change_vs_reference"]
        ),
        "duplicate_reversal_postings": sum(1 for r in records if r["duplicate_reversal_posting"]),
        "ledger_ok": ledger["ok"],
        "audit_chain_ok": chain.get("ok"),
        "audit_rows_checked": chain.get("checked"),
        "audit_rejections": sum(a["count"] for a in audit if a["decision"] == "REJECTED"),
    }
    outputs = {
        "cases_csv": write_csv(edir / "raw" / f"{run_id}-cases.csv", records),
        "raw_json": write_json(
            edir / "raw" / f"{run_id}.json",
            {"ledger": ledger, "audit_chain": chain, "audit_actions": audit, "row": row},
        ),
    }
    return outputs, row


def cmd_replay(args, env) -> None:
    out = Path(args.out)
    man = Manifest(out, "replay", env)
    edir = out / "replay"
    rows = []
    for seed in args.seeds:
        for auto in (True, False):
            status, row = man.run(
                "D-transaction-replay",
                f"auto_reverse={auto}",
                seed,
                {"auto_reverse_on_confirmed_failure": auto},
                lambda rid, s=seed, a=auto: replay_once(s, a, rid, edir),
            )
            print(f"replay seed={seed} auto_reverse={auto}: {status} {row}", flush=True)
            if row:
                rows.append({**row, "experiment_id": man.experiment_id})
    write_csv(edir / f"{man.experiment_id}-runs.csv", rows)
    write_json(
        edir / f"{man.experiment_id}-summary.json",
        {
            "experiment_id": man.experiment_id,
            "seeds": args.seeds,
            "note": "The replay cases are deterministic; repetitions check that the outcome is stable.",
            "totals": {
                k: sum(r[k] for r in rows)
                for k in (
                    "cases",
                    "http_as_expected",
                    "unexpected_financial_changes",
                    "duplicate_reversal_postings",
                )
            },
            "all_ledgers_ok": all(r["ledger_ok"] for r in rows),
            "all_audit_chains_ok": all(r["audit_chain_ok"] for r in rows),
            "runs": rows,
        },
    )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "command",
        choices=["environment", "calibrate", "flooding", "poisoning", "poisoning-live", "replay"],
    )
    p.add_argument("--out", default=str(OUT))
    p.add_argument("--seeds", type=int, nargs="+", default=DEFAULT_SEEDS)
    p.add_argument("--rates", type=float, nargs="+", default=[25, 50, 100, 150, 200, 300])
    p.add_argument("--attack-rps", type=float, default=None)
    p.add_argument("--conditions", nargs="+", default=list(FLOOD_CONDITIONS))
    p.add_argument("--cases", nargs="+", default=list(LIVE_CASES))
    args = p.parse_args()
    out = Path(args.out)
    if args.command == "calibrate":
        cmd_calibrate(args)
        return
    env = environment()
    write_json(out / f"environment-{args.command}.json", env)
    if args.command == "environment":
        print(json.dumps(env, indent=2))
        return
    {
        "flooding": cmd_flooding,
        "poisoning": cmd_poisoning,
        "poisoning-live": cmd_poisoning_live,
        "replay": cmd_replay,
    }[args.command](args, env)


if __name__ == "__main__":
    main()
