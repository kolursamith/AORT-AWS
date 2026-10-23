"""Stage 10: Track B's three attacks against the integrated AORTS v1.1 loop.

Every run builds a fresh in-process Track B stack (real HTTP handlers, real HMAC signing, real
PostgreSQL on the disposable ``aorts_experiments`` database), couples it to a real Track A
controller through :class:`~backend.aorts.integration.loop.AortsLoop`, warms the controller up
on honest traffic, and then runs one condition. Synthetic data only; nothing here contacts a
real bank, NPCI/UPI or any public system.

    python -m backend.aorts.experiments.stage10_security flooding   --seeds 101 102 103 104 105
    python -m backend.aorts.experiments.stage10_security poisoning  --seeds 101 102 103 104 105
    python -m backend.aorts.experiments.stage10_security resilience --seeds 101 102 103 104 105
    python -m backend.aorts.experiments.stage10_security replay     --seeds 101 102 103 104 105
    python -m backend.aorts.experiments.stage10_security summary

Reused unchanged from Track B: the A3 flooding generator (``run_flood``), the flooding pools
(``build_pool``), the telemetry validator, the replay simulator, the run manifest (``Manifest``:
a ``start`` and an ``end`` line per run, failed runs kept) and the statistics helpers.

Compact results (runs CSV, summaries, manifest) go to ``docs/integration/evidence/stage10``;
per-tick and per-request raw files go to ``results/processed/stage10`` (not in Git).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import threading
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from sqlalchemy import text

from backend.aorts.app.local_stack import generate_service_keys
from backend.aorts.app.metrics import percentile
from backend.aorts.contracts.requests import DefenseConfig, FaultConfig
from backend.aorts.experiments.common import ROOT, inprocess_stack
from backend.aorts.experiments.run_flooding import build_pool
from backend.aorts.experiments.security_package import (
    FLOOD_CAPACITY,
    FLOOD_CONDITIONS,
    Manifest,
    aggregate,
    environment,
    paired_diff,
    sha256_file,
    write_csv,
    write_json,
)
from backend.aorts.integration.loop import (
    AortsLoop,
    ControllerFaults,
    LoopConfig,
    Workload,
    ambiguous_backlog,
)
from backend.aorts.integration.v11 import telemetry_source
from backend.aorts.security.flooding_simulator import FloodConfig, run_flood
from backend.aorts.security.replay_simulator import ReplaySimulator
from backend.aorts.security.telemetry_validator import TelemetryValidator

OUT = ROOT / "docs" / "integration" / "evidence" / "stage10"
RAW = ROOT / "results" / "processed" / "stage10"
SEEDS = [101, 102, 103, 104, 105]
# Track B's calibrated flooding capacity, plus a fixed check spacing so Track B's timing is
# expressible as one v1.1 timing policy (exponential backoff is not).
# deadline_seconds: long enough that the pre-built backlog is still recoverable at the end of a
# scenario (Track B's default 120 s would escalate it to HANDOFF_UDIR mid-run).
STACK = {
    **FLOOD_CAPACITY,
    "check_backoff_base_seconds": 1.0,
    "check_backoff_max_seconds": 1.0,
    "deadline_seconds": 900,
}
WARM = {"warm_s": 14, "warm_rps": 12.0, "settle_max_s": 24, "rps": 3.0, "backlog_per_route": 30}
TIMELINE = {"lead_s": 4.0, "attack_s": 8.0, "tail_s": 10.0}
FLOOD_RATES = [10.0, 50.0, 100.0, 150.0]  # 150: Track B's calibrated harmful rate
ROUTES = ("A1-B", "A1-C")


def source_digest() -> str:
    """SHA-256 over every Python file of the code that runs (Track B, Track A, contracts)."""

    h = hashlib.sha256()
    for base in ("backend/aorts", "intelligence", "contracts"):
        for p in sorted((ROOT / base).rglob("*.py")):
            h.update(p.relative_to(ROOT).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()


class Campaign(Manifest):
    """Track B's :class:`Manifest`, plus a per-run row file and resumption of an interrupted
    campaign (``--resume <experiment_id>``).

    A run is reused only when its manifest ``end`` line belongs to the same experiment id, is
    ``completed``, was produced from the same source digest and configuration, and its row file
    still has the recorded hash. Everything else runs again; the manifest is append-only.
    """

    def __init__(self, out: Path, experiment: str, env: dict, raw: Path, resume: str | None):
        super().__init__(out, experiment, env)
        self.raw = raw
        self.done: dict[tuple, dict] = {}
        if not resume:
            return
        self.experiment_id = resume
        for line in self.path.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            row_file = (rec.get("outputs") or {}).get("row")
            if (
                rec["experiment_id"] != resume
                or rec["event"] != "end"
                or rec["status"] != "completed"
                or rec["source_sha256"] != env["source_sha256"]
                or not row_file
            ):
                continue
            path = ROOT / row_file["path"]
            if path.exists() and sha256_file(path) == row_file["sha256"]:
                key = (rec["condition"], rec["seed"], _canon(rec["config"]))
                self.done[key] = json.loads(path.read_text(encoding="utf-8"))

    def run(self, scenario: str, condition: str, seed: int, config: dict, fn):
        key = (condition, seed, _canon(config))
        if key in self.done:
            print(f"  resumed {condition} seed={seed} from its recorded row", flush=True)
            return "completed", self.done[key]

        def with_row(file_key: str):
            outputs, row = fn(file_key)
            return {**outputs, "row": write_json(self.raw / f"{file_key}-row.json", row)}, row

        return super().run(scenario, condition, seed, config, with_row)


def _canon(config: dict) -> str:
    return json.dumps(config, sort_keys=True, default=str)


@contextmanager
def stack(experiment_id: str, **overrides):
    with inprocess_stack(
        **{**STACK, **overrides},
        experiment_id=experiment_id,
        service_keys=generate_service_keys(),  # ephemeral, in memory only
        admin_token=secrets.token_urlsafe(24),
    ) as st:
        yield st


def steps(loop: AortsLoop, seconds: float, phase: str) -> list[dict]:
    """Step the loop for ``seconds`` (the workload keeps running in its own thread)."""

    first, end = len(loop.ticks), time.time() + seconds
    while time.time() < end:
        loop.step()["phase"] = phase
    return loop.ticks[first:]


def ready(loop: AortsLoop) -> bool:
    ticks = loop.ticks[-1]["routes"]
    return all(
        ticks[r].get("outcome") == "DECISION" and (ticks[r].get("budget_rps") or 0) > 0
        for r in ROUTES
    )


def warm_up(loop: AortsLoop, wl: Workload) -> dict:
    """Honest high load (1 s windows) demonstrates capacity; then calm 2 s windows until both
    routes have a positive controller decision, or ``settle_max_s`` passes (recorded)."""

    wl.set_rate({"B": WARM["warm_rps"], "C": WARM["warm_rps"]})
    steps(loop, WARM["warm_s"], "warm")
    wl.set_rate({"B": WARM["rps"], "C": WARM["rps"]})
    loop.cfg.window_s = 2
    t0, deadline = time.time(), time.time() + WARM["settle_max_s"]
    ok = False
    while time.time() < deadline:
        loop.step()["phase"] = "settle"
        if ready(loop):
            ok = True
            break
    return {"ready": ok, "settle_s": round(time.time() - t0, 2)}


def _windowed(ticks: list[dict], start: float, lead: float, dur: float):
    def window(t):
        rel = t - start
        return (
            None
            if rel < 0
            else ("before" if rel < lead else "during" if rel < lead + dur else "after")
        )

    return window


def aorts_metrics(ticks: list[dict], window, prefix: str = "") -> dict:
    """Controller and dispatcher behaviour per route and timeline window, from the loop ticks."""

    row: dict = {}
    for rid in ROUTES:
        tag = rid.replace("A1-", "")
        for w in ("before", "during", "after"):
            ts = [t for t in ticks if window(t["t"]) == w]
            rs = [t["routes"][rid] for t in ts]
            budgets = [r.get("budget_rps") for r in rs if r.get("budget_rps") is not None]
            row[f"{prefix}{tag}_dispatched_{w}"] = sum(r.get("dispatched") or 0 for r in rs)
            row[f"{prefix}{tag}_budget_mean_{w}"] = (
                round(sum(budgets) / len(budgets), 3) if budgets else None
            )
            row[f"{prefix}{tag}_positive_ticks_{w}"] = sum(1 for b in budgets if b > 0)
            row[f"{prefix}{tag}_ticks_{w}"] = len(rs)
            row[f"{prefix}{tag}_trusted_windows_{w}"] = sum(
                1 for r in rs if r.get("verdict") == "TRUSTED"
            )
            row[f"{prefix}{tag}_flagged_windows_{w}"] = sum(
                1 for r in rs if r.get("verdict") not in (None, "TRUSTED")
            )
            for src in ("controller", "trackb_fallback", "fail_closed_honoured"):
                row[f"{prefix}{tag}_{src}_ticks_{w}"] = sum(1 for r in rs if r.get("source") == src)
            row[f"{prefix}{tag}_fallback_dispatched_{w}"] = sum(
                r.get("dispatched") or 0 for r in rs if r.get("source") == "trackb_fallback"
            )
            row[f"{prefix}{tag}_failclosed_dispatched_{w}"] = sum(
                r.get("dispatched") or 0 for r in rs if r.get("source") == "fail_closed_honoured"
            )
    return row


def safety(st) -> dict:
    with st.db.session() as s:
        q = lambda sql: s.execute(text(sql)).scalar()  # noqa: E731
        return {
            "ledger_ok": st.ledger.verify_ledger()["ok"],
            "duplicate_check_starts": q(
                "SELECT count(*) FROM (SELECT transaction_id, previous_version FROM "
                "transaction_events WHERE event_type='CHECK_STARTED' GROUP BY 1, 2 "
                "HAVING count(*) > 1) d"
            ),
            "multiple_reversals": q(
                "SELECT count(*) FROM (SELECT transaction_id FROM financial_operations WHERE "
                "operation_type='REVERSAL' GROUP BY 1 HAVING count(*) > 1) d"
            ),
            "multiple_debits": q(
                "SELECT count(*) FROM (SELECT transaction_id FROM financial_operations WHERE "
                "operation_type='DEBIT' GROUP BY 1 HAVING count(*) > 1) d"
            ),
            "unverified_evidence_applied": q(
                "SELECT count(*) FROM transaction_events te JOIN evidence_records er "
                "ON er.evidence_id = te.metadata->>'evidence_id' WHERE NOT er.verified AND "
                "te.event_type IN ('PAYMENT_SUCCEEDED', 'PAYMENT_FAILED', 'PAYMENT_CLOSED', "
                "'REVERSAL_COMPLETED')"
            ),
        }


def _p(values, q):
    v = percentile(values, q)
    return None if v is None else round(v, 2)


def _ctr_delta(after: dict, before: dict, prefix: str, bank: str) -> float:
    return sum(
        v - before.get(k, 0)
        for k, v in after.items()
        if k.startswith(prefix) and f"bank={bank}" in k
    )


# ------------------------------------------------------------------------- flooding


def flood_once(condition: str, seed: int, rate: float, key: str, raw: Path):
    attack, defended, dest = FLOOD_CONDITIONS[condition]
    exp = f"s10-flood-{key}"
    lead, dur, tail = TIMELINE["lead_s"], TIMELINE["attack_s"], TIMELINE["tail_s"]
    with stack(exp) as st:
        st.set_defenses(DefenseConfig(rate_limiting=defended, tier2_credits=defended))
        n = WARM["backlog_per_route"]
        backlog = ambiguous_backlog(st, {"B": n, "C": n})
        admin = st.settings.admin_token.get_secret_value()
        pool = build_pool(st.client, admin, {"A3": "B", "A1": dest, "A2": dest})
        loop = AortsLoop(st, LoopConfig(experiment_id=exp, window_s=1))
        wl = Workload(st, seed=seed)
        warm = warm_up(loop, wl)
        sw = st.switch_service
        sw.admission.reset()  # as Track B's package: quotas and credits start full
        before = dict(sw.ctx.metrics.snapshot()["counters"])
        unresolved_start = loop.unresolved()
        start = time.time() - loop.t0
        wl_start = len(wl.records)
        res: dict = {}
        cfg = FloodConfig(
            attack_rps=rate if attack else 0.0,
            duration_s=dur,
            legit_rps_per_bank=3.0,
            destination="B",
            seed=seed,
            lead_s=lead,
            tail_s=tail,
        )
        fl = threading.Thread(target=lambda: res.update(r=run_flood(st.client, pool, cfg)))
        fl.start()
        end = time.time() + lead + dur + tail
        while fl.is_alive() or time.time() < end:
            loop.step()["phase"] = "timeline"
        wl.close()
        after = dict(sw.ctx.metrics.snapshot()["counters"])
        unresolved_end = loop.unresolved()
        safe = safety(st)
        bank_limit = st.settings.bank_check_rate_per_sec
        quotas = {b: sw.admission.quota_view(b) for b in ("A1", "A2", "A3")}
    result = res["r"]
    summ = result.summary(bank_limit=bank_limit)
    window = _windowed(loop.ticks, start, lead, dur)
    recs = result.records
    row = {
        "condition": condition,
        "seed": seed,
        "attack_rps": rate if attack else 0.0,
        "defenses": "on" if defended else "off",
        "legit_destination": dest,
        **{f"warm_{k}": v for k, v in warm.items()},
        "backlog_created": len(backlog),
    }

    def rwin(r):
        return "before" if r.t < lead else ("during" if r.t < lead + dur else "after")

    legit = [r for r in recs if r.bank != "A3"]
    for w in ("before", "during", "after"):
        rs = [r for r in legit if rwin(r) == w]
        ok = [r.latency_ms for r in rs if r.status == 200]
        row[f"legit_check_success_{w}"] = round(len(ok) / len(rs), 4) if rs else None
        row[f"legit_check_p95_ms_{w}"] = _p(ok, 0.95)
        row[f"legit_check_p99_ms_{w}"] = _p(ok, 0.99)
    for b in ("A1", "A2", "A3"):
        pb = summ["per_bank"].get(b, {})
        row[f"{b}_sent"] = pb.get("sent", 0)
        row[f"{b}_accepted"] = pb.get("admitted_ok", 0)
        row[f"{b}_rejected"] = sum(1 for r in recs if r.bank == b and r.status == 429)
        row[f"{b}_server_received"] = _ctr_delta(after, before, "check_received", b)
        row[f"{b}_server_admitted"] = _ctr_delta(after, before, "check_admitted", b)
        row[f"{b}_reached_worker_pool"] = _ctr_delta(
            after, before, "check_completed", b
        ) + _ctr_delta(after, before, "core_overloaded", b)
        row[f"{b}_credits_left"] = quotas[b]["credits"]
    for code in (
        "RATE_LIMITED",
        "GLOBAL_CAPACITY_EXCEEDED",
        "ROUTE_RATE_LIMITED",
        "CREDIT_EXHAUSTED",
    ):
        row[f"A3_rejected_{code}"] = sum(
            v - before.get(k, 0)
            for k, v in after.items()
            if k.startswith("check_rejected{") and "bank=A3" in k and f"reason={code}" in k
        )
    row["detection_s"] = summ["attack_detection_s"]
    row["containment_s"] = summ["attack_containment_s"]
    row["recovery_after_attack_s"] = summ.get("recovery_after_attack_s")
    during = [r for r in recs if lead <= r.t < lead + dur and r.status == 200]
    row["attacker_share_of_admitted_during"] = (
        round(sum(1 for r in during if r.bank == "A3") / len(during), 4) if during else None
    )
    pays = wl.records[wl_start:]
    for w in ("before", "during", "after"):
        ps = [p for p in pays if window(p["t"] + (wl.t0 - loop.t0)) == w]
        for dest_ in ("B", "C"):
            ok = [
                p["latency_ms"] for p in ps if p["destination"] == dest_ and p["state"] == "CLOSED"
            ]
            span = lead if w == "before" else dur if w == "during" else tail
            row[f"payments_closed_per_s_{dest_}_{w}"] = round(len(ok) / span, 3)
            row[f"payment_p95_ms_{dest_}_{w}"] = _p(ok, 0.95)
            row[f"payment_p99_ms_{dest_}_{w}"] = _p(ok, 0.99)
    row["payments_sent"] = len(pays)
    row["payments_not_closed"] = sum(1 for p in pays if p["state"] != "CLOSED")
    row["unresolved_start"] = unresolved_start
    row["unresolved_end"] = unresolved_end
    row.update(aorts_metrics(loop.ticks, window))
    # controller recovery: first tick after the attack with a positive budget on route B
    end_attack = start + lead + dur
    later = [t for t in loop.ticks if t["t"] >= end_attack]
    first = next((t for t in later if (t["routes"]["A1-B"].get("budget_rps") or 0) > 0), None)
    row["controller_B_recovery_s"] = round(first["t"] - end_attack, 2) if first else None
    row.update(safe)
    outputs = {
        "ticks": write_json(raw / f"{key}-ticks.json", loop.ticks),
        "requests": write_csv(
            raw / f"{key}-requests.csv", [asdict(r) for r in sorted(recs, key=lambda r: r.t)]
        ),
        "payments": write_csv(raw / f"{key}-payments.csv", pays or [{"t": None}]),
        "summary": write_json(raw / f"{key}.json", {"row": row, "flood_summary": summ}),
    }
    return outputs, row


def cmd_flooding(args, env) -> None:
    out = Path(args.out) / "flooding"
    raw = RAW / "flooding"
    m = Campaign(out, "stage10-flooding", env, raw, args.resume)
    rows = []
    plan = [(c, FLOOD_RATES[-1]) for c in FLOOD_CONDITIONS]
    plan += [(c, r) for r in FLOOD_RATES[:-1] for c in ("flood_undefended", "flood_defended")]
    for seed in args.seeds:
        for cond, rate in plan:
            config = {"stack": STACK, "warm": WARM, "timeline": TIMELINE, "attack_rps": rate}
            status, row = m.run(
                "S10-FLOOD",
                cond,
                seed,
                config,
                lambda k, c=cond, r=rate, s=seed: flood_once(c, s, r, k, raw),
            )
            print(f"flood {cond} rate={rate} seed={seed}: {status}", flush=True)
            if row:
                rows.append({**row, "status": status})
    _finish(out, m, rows, ["condition", "attack_rps"], FLOOD_METRICS)


FLOOD_METRICS = [
    "legit_check_success_during",
    "legit_check_p95_ms_during",
    "A3_accepted",
    "A3_rejected",
    "containment_s",
    "recovery_after_attack_s",
    "payments_closed_per_s_B_during",
    "payments_closed_per_s_C_during",
    "payment_p95_ms_B_during",
    "payment_p99_ms_B_during",
    "B_dispatched_during",
    "C_dispatched_during",
    "B_budget_mean_before",
    "B_budget_mean_during",
    "C_budget_mean_during",
    "controller_B_recovery_s",
    "unresolved_end",
]


# ------------------------------------------------------------------------ poisoning

LIES = {
    "inflate_30": lambda r: {"throughput_tps": (r.get("throughput_tps") or 0) * 1.3},
    "inflate_100": lambda r: {"throughput_tps": (r.get("throughput_tps") or 0) * 2.0},
    "latency_low": lambda r: {
        k: (r.get(k) or 0) * 0.2 for k in ("latency_p50_ms", "latency_p95_ms")
    },
    "timeout_low": lambda r: {"timeout_rate": 0.0},
    "backlog_inconsistent": lambda r: {"backlog": 0 if int(time.time()) % 4 < 2 else 400},
    "conflicting": lambda r: {
        "timeout_rate": 0.0,
        "backlog": 0,
        "throughput_tps": max(r.get("throughput_tps") or 0, 40.0),
        "latency_p50_ms": min(r.get("latency_p50_ms") or 0, 20.0),
        "latency_p95_ms": min(r.get("latency_p95_ms") or 0, 30.0),
    },
}
ARMS = ("clean", "poisoned_unprotected", "poisoned_protected")
DEGRADE = [  # an unannounced drop on route B at attack start, in every arm
    FaultConfig(target="route:B", capacity_workers=1),
    FaultConfig(target="beneficiary:B", latency_ms=500),
]


def poison_once(case: str, arm: str, seed: int, key: str, raw: Path):
    exp = f"s10-poison-{key}"
    lead, dur, tail = TIMELINE["lead_s"], 12.0, TIMELINE["tail_s"]
    validator = TelemetryValidator(
        sources={telemetry_source(r) for r in ROUTES}, enabled=(arm != "poisoned_unprotected")
    )
    with stack(exp) as st:
        n = WARM["backlog_per_route"]
        ambiguous_backlog(st, {"B": n, "C": n})
        loop = AortsLoop(st, LoopConfig(experiment_id=exp, window_s=1), validator=validator)
        wl = Workload(st, seed=seed)
        warm = warm_up(loop, wl)
        start = time.time() - loop.t0
        steps(loop, lead, "before")
        for f in DEGRADE:
            st.set_fault(f)
        if arm != "clean":
            loop.overrides["A1-B"] = LIES[case]
        steps(loop, dur, "during")
        st.reset_faults()
        loop.overrides.clear()
        steps(loop, tail, "after")
        wl.close()
        safe = safety(st)
    window = _windowed(loop.ticks, start, lead, dur)
    during = [t for t in loop.ticks if window(t["t"]) == "during"]
    b = [t["routes"]["A1-B"] for t in during]
    first_flag = next(
        (i for i, r in enumerate(b) if r.get("verdict") not in (None, "TRUSTED")), None
    )
    first_zero = next((i for i, r in enumerate(b) if not (r.get("budget_rps") or 0)), None)
    pays = [p for p in wl.records if window(p["t"] + (wl.t0 - loop.t0)) == "during"]
    pb = [p["latency_ms"] for p in pays if p["destination"] == "B"]
    row = {
        "condition": f"{case}:{arm}",
        "case": case,
        "arm": arm,
        "seed": seed,
        **{f"warm_{k}": v for k, v in warm.items()},
        "B_ticks_during": len(b),
        "B_flagged_during": sum(1 for r in b if r.get("verdict") not in (None, "TRUSTED")),
        "B_detection_rate": round(
            sum(1 for r in b if r.get("verdict") not in (None, "TRUSTED")) / len(b), 4
        )
        if b
        else None,
        "B_detection_tick_offset": first_flag,
        "B_budget_zero_tick_offset": first_zero,
        "B_positive_budget_ticks_during": sum(1 for r in b if (r.get("budget_rps") or 0) > 0),
        "B_max_budget_during": max((r.get("budget_rps") or 0 for r in b), default=0),
        "B_max_headroom_during": max((r.get("headroom_rps") or 0 for r in b), default=0),
        "B_dispatched_during": sum(r.get("dispatched") or 0 for r in b),
        "B_predispatch_refused_during": sum(
            sum((r.get("predispatch_refused") or {}).values()) for r in b
        ),
        "B_gate_refused_during": sum(len(r.get("gate_refused") or {}) for r in b),
        "B_payment_p95_ms_during": _p(pb, 0.95),
        "B_payments_not_closed_during": sum(
            1 for p in pays if p["destination"] == "B" and p["state"] != "CLOSED"
        ),
        **aorts_metrics(loop.ticks, window),
        **safe,
    }
    outputs = {"ticks": write_json(raw / f"{key}-ticks.json", loop.ticks)}
    return outputs, row


def protocol_once(seed: int, key: str, raw: Path):
    """Protocol-level poisoning (replayed, stale, mis-attributed, malformed, missing windows)
    sent through the controller's real intake, after an honest warm-up."""

    from sqlalchemy import select

    from backend.aorts.banking.ledger.models import TelemetryWindow
    from backend.aorts.integration.v11 import export_window
    from contracts.v1_1.protocol import encode

    exp = f"s10-proto-{key}"
    cases = []
    with stack(exp) as st:
        n = WARM["backlog_per_route"]
        ambiguous_backlog(st, {"B": n, "C": n})
        loop = AortsLoop(st, LoopConfig(experiment_id=exp, window_s=1))
        wl = Workload(st, seed=seed)
        warm = warm_up(loop, wl)
        hdr = {"Authorization": f"Bearer {loop.token}"}

        def seq():
            snap = loop.core._route_twin.snapshot(exp, "A1-B")
            return {m.source_id: m.sequence for m in snap.latest_sequence_per_source}.get(
                telemetry_source("A1-B")
            )

        def post_raw(body: bytes):
            return loop.client.post("/v1.1/telemetry", content=body, headers=hdr)

        with st.db.session() as s:
            rows = (
                s.execute(
                    select(TelemetryWindow)
                    .where(TelemetryWindow.route_id == "A1-B", TelemetryWindow.verdict == "TRUSTED")
                    .order_by(TelemetryWindow.sequence)
                )
                .scalars()
                .all()
            )
            latest, older = rows[-1], rows[max(0, len(rows) - 4)]
            for r in (latest, older):
                s.expunge(r)
        good = export_window(latest).value
        before = seq()
        variants = {
            "replayed_signed_observation": encode(good),
            "stale_sequence": encode(export_window(older).value),
            "wrong_source_identity": encode(
                good.model_copy(update={"telemetry": good.telemetry.model_copy(
                    update={"source_id": telemetry_source("A1-C")})})
            ),
            "unregistered_source": encode(
                good.model_copy(update={"telemetry": good.telemetry.model_copy(
                    update={"source_id": "rogue-probe"})})
            ),
            "other_experiment": encode(good.model_copy(update={"experiment_id": "other-exp"})),
        }  # fmt: skip
        body = json.loads(encode(good))
        body["telemetry"].pop("observation_window_start", None)
        variants["missing_observation_window"] = json.dumps(body).encode()
        for name, payload in variants.items():
            r = post_raw(payload)
            code = r.json().get("error_code") if r.content else None
            cases.append({"case": name, "status": r.status_code, "error_code": code,
                          "applied": r.status_code == 204, "twin_sequence_unchanged": seq() == before})  # fmt: skip
        # the source goes silent, then returns: no unsafe budget while silent
        loop.telemetry_enabled = False
        silent = steps(loop, 10.0, "silent")
        loop.telemetry_enabled = True
        back = steps(loop, 16.0, "returned")
        wl.close()
        safe = safety(st)

    def b(t):
        return t["routes"]["A1-B"]

    cases.append({
        "case": "source_silent_10s",
        "positive_budget_ticks": sum(1 for t in silent if (b(t).get("budget_rps") or 0) > 0),
        "last_tick_outcome": b(silent[-1]).get("outcome") if silent else None,
        "last_tick_reason": b(silent[-1]).get("decision_reason") if silent else None,
        "fallback_dispatched": sum(b(t).get("dispatched") or 0 for t in silent
                                   if b(t).get("source") == "trackb_fallback"),
    })  # fmt: skip
    first_pos = next((t for t in back if (b(t).get("budget_rps") or 0) > 0), None)
    cases.append({
        "case": "source_returns",
        "time_to_positive_budget_s": round(first_pos["t"] - back[0]["t"], 2) if first_pos else None,
        "outcomes": dict(Counter(str(b(t).get("decision_reason")) for t in back)),
    })  # fmt: skip
    refused = [c for c in cases if "applied" in c]
    row = {
        "condition": "protocol",
        "seed": seed,
        **{f"warm_{k}": v for k, v in warm.items()},
        "protocol_cases": len(refused),
        "protocol_applied": sum(1 for c in refused if c["applied"]),
        "protocol_twin_changed": sum(1 for c in refused if not c["twin_sequence_unchanged"]),
        **{f"{c['case']}:status": c["status"] for c in refused},
        **{f"{c['case']}:error_code": c["error_code"] for c in refused},
        "silent_positive_budget_ticks": cases[-2]["positive_budget_ticks"],
        "silent_last_reason": cases[-2]["last_tick_reason"],
        "silent_fallback_dispatched": cases[-2]["fallback_dispatched"],
        "return_time_to_positive_s": cases[-1]["time_to_positive_budget_s"],
        **safe,
    }
    outputs = {"cases": write_json(raw / f"{key}-protocol.json", cases),
               "ticks": write_json(raw / f"{key}-ticks.json", loop.ticks)}  # fmt: skip
    return outputs, row


def cmd_poisoning(args, env) -> None:
    out = Path(args.out) / "poisoning"
    raw = RAW / "poisoning"
    m = Campaign(out, "stage10-poisoning", env, raw, args.resume)
    rows = []
    cases = args.cases or list(LIES)
    for seed in args.seeds:
        status, row = m.run(
            "S10-POISON-PROTOCOL", "protocol", seed, {"stack": STACK, "warm": WARM},
            lambda k, s=seed: protocol_once(s, k, raw),
        )  # fmt: skip
        print(f"poison protocol seed={seed}: {status}", flush=True)
        if row:
            rows.append({**row, "status": status})
        for case in cases:
            for arm in ARMS:
                config = {
                    "stack": STACK,
                    "warm": WARM,
                    "degrade": [d.model_dump() for d in DEGRADE],
                }
                status, row = m.run(
                    "S10-POISON", f"{case}:{arm}", seed, config,
                    lambda k, c=case, a=arm, s=seed: poison_once(c, a, s, k, raw),
                )  # fmt: skip
                print(f"poison {case} {arm} seed={seed}: {status}", flush=True)
                if row:
                    rows.append({**row, "status": status})
    _finish(out, m, rows, ["condition"], POISON_METRICS)


POISON_METRICS = [
    "B_detection_rate",
    "B_detection_tick_offset",
    "B_positive_budget_ticks_during",
    "B_max_budget_during",
    "B_dispatched_during",
    "B_payment_p95_ms_during",
    "C_dispatched_during",
]


# ----------------------------------------------------------------------- resilience

PHASES = [
    # (phase, how to inject, how to clear); every phase runs PHASE_S of calm traffic
    ("baseline", None),
    ("controller_down", ("proxy", "down")),
    ("controller_http_500", ("proxy", "http_500")),
    ("controller_slow_within_timeout", ("proxy", "slow", 0.5)),
    ("controller_timeout", ("proxy", "slow", 3.0)),
    ("malformed_response", ("proxy", "malformed")),
    ("wrong_protocol_version", ("proxy", "wrong_version")),
    ("wrong_route_id", ("proxy", "wrong_route")),
    ("recovered_after_faults", None),
    ("explicit_fail_closed_latch", ("latch",)),
    ("latch_released", ("release",)),
    ("poisoned_telemetry_inflate_100", ("lie", "inflate_100")),
    ("source_disconnected", ("telemetry_off",)),
    ("observations_return", ("telemetry_on",)),
    ("event_delivery_backlog", ("events_off",)),
    ("event_backlog_drained", ("events_on",)),
    ("controller_restart", ("restart",)),
    ("after_restart", None),
]
PHASE_S = 6.0


def resilience_once(seed: int, key: str, raw: Path):
    exp = f"s10-resil-{key}"
    with stack(exp) as st:
        n = 150  # the labelled fallback (2 per tick and route) must not drain it mid-scenario
        ambiguous_backlog(st, {"B": n, "C": n})
        loop = AortsLoop(st, LoopConfig(experiment_id=exp, window_s=1))
        proxy = ControllerFaults(lambda: loop.client)
        loop.dispatch_http = proxy.client
        loop.dispatcher.v11 = loop.v11_client(http=proxy.client)
        wl = Workload(st, seed=seed)
        warm = warm_up(loop, wl)
        phase_rows = []
        for name, action in PHASES:
            proxy.mode, proxy.delay_s = None, 0.0
            if action:
                kind = action[0]
                if kind == "proxy":
                    proxy.mode = action[1]
                    proxy.delay_s = action[2] if len(action) > 2 else 0.0
                elif kind == "latch":
                    for r in ROUTES:
                        loop.core._routes[r].estimator.latch_fail_closed()
                elif kind == "release":
                    for r in ROUTES:
                        loop.core._routes[r].estimator.release_fail_closed()
                elif kind == "lie":
                    loop.overrides["A1-B"] = LIES[action[1]]
                elif kind == "telemetry_off":
                    loop.overrides.clear()
                    loop.telemetry_enabled = False
                elif kind == "telemetry_on":
                    loop.telemetry_enabled = True
                elif kind == "events_off":
                    loop.events_enabled = False
                elif kind == "events_on":
                    loop.events_enabled = True
                elif kind == "restart":
                    loop.restart_controller()
            ticks = steps(loop, PHASE_S, name)
            if action and action[0] == "lie":
                loop.overrides.clear()
            for rid in ROUTES:
                rs = [t["routes"][rid] for t in ticks]
                outcomes = Counter(str(r.get("outcome")) for r in rs)
                sources = Counter(str(r.get("source")) for r in rs)
                phase_rows.append(
                    {
                        "phase": name,
                        "route": rid,
                        "ticks": len(rs),
                        "outcomes": dict(outcomes),
                        "sources": dict(sources),
                        "dispatched": sum(r.get("dispatched") or 0 for r in rs),
                        "fallback_dispatched": sum(
                            r.get("dispatched") or 0
                            for r in rs
                            if r.get("source") == "trackb_fallback"
                        ),
                        "fail_closed_dispatched": sum(
                            r.get("dispatched") or 0
                            for r in rs
                            if r.get("source") == "fail_closed_honoured"
                        ),
                        "max_dispatched_per_tick": max(
                            (r.get("dispatched") or 0 for r in rs), default=0
                        ),
                        "positive_budget_ticks": sum(
                            1 for r in rs if (r.get("budget_rps") or 0) > 0
                        ),
                        "decision_reasons": dict(
                            Counter(str(r.get("decision_reason")) for r in rs)
                        ),
                        "verdicts": dict(Counter(str(r.get("verdict")) for r in rs)),
                        "first_positive_tick": next(
                            (i for i, r in enumerate(rs) if (r.get("budget_rps") or 0) > 0), None
                        ),
                    }
                )
        wl.close()
        safe = safety(st)
        fallback_cap = st.settings.fallback_budget_per_route
    fail_closed_fallback = sum(
        p["fallback_dispatched"]
        for p in phase_rows
        if p["phase"] in ("explicit_fail_closed_latch", "source_disconnected")
    )
    row = {
        "condition": "resilience",
        "seed": seed,
        **{f"warm_{k}": v for k, v in warm.items()},
        "fallback_cap_per_tick": fallback_cap,
        "max_fallback_per_tick": max(
            (
                p["max_dispatched_per_tick"]
                for p in phase_rows
                if p["sources"].get("trackb_fallback")
            ),
            default=0,
        ),
        "fail_closed_phases_fallback_dispatched": fail_closed_fallback,
        "fail_closed_dispatched_total": sum(p["fail_closed_dispatched"] for p in phase_rows),
        **{
            f"{p['phase']}:{p['route']}:sources": json.dumps(p["sources"], sort_keys=True)
            for p in phase_rows
        },
        **{f"{p['phase']}:{p['route']}:dispatched": p["dispatched"] for p in phase_rows},
        **safe,
    }
    outputs = {
        "phases": write_json(raw / f"{key}-phases.json", phase_rows),
        "ticks": write_json(raw / f"{key}-ticks.json", loop.ticks),
    }
    return outputs, row


def cmd_resilience(args, env) -> None:
    out = Path(args.out) / "resilience"
    raw = RAW / "resilience"
    m = Campaign(out, "stage10-resilience", env, raw, args.resume)
    rows = []
    for seed in args.seeds:
        config = {
            "stack": STACK,
            "warm": WARM,
            "phases": [p[0] for p in PHASES],
            "phase_s": PHASE_S,
        }
        status, row = m.run(
            "S10-RESIL", "resilience", seed, config, lambda k, s=seed: resilience_once(s, k, raw)
        )
        print(f"resilience seed={seed}: {status}", flush=True)
        if row:
            rows.append({**row, "status": status})
    _finish(out, m, rows, ["condition"], ["fail_closed_dispatched_total", "max_fallback_per_tick"])


# --------------------------------------------------------------------------- replay


def replay_once(seed: int, auto_reverse: bool, key: str, raw: Path):
    """Track B's replay simulator on the integrated stack, then the v1.1 replay cases."""

    from backend.aorts.integration.replay_v11 import run_v11_replay_cases

    exp = f"s10-replay-{key}"
    with stack(exp, auto_reverse_on_confirmed_failure=auto_reverse) as st:
        trackb = ReplaySimulator(st.client, st.db, st.settings.admin_token.get_secret_value())
        cases = trackb.run_all()
        loop = AortsLoop(st, LoopConfig(experiment_id=exp, window_s=1, estimator=None))
        v11_cases = run_v11_replay_cases(st, loop, seed=seed)
        safe = safety(st)
    unexpected = [c for c in v11_cases if not c["as_expected"]]
    row = {
        "condition": f"auto_reverse={auto_reverse}",
        "seed": seed,
        "trackb_cases": len(cases),
        "trackb_duplicate_reversal_postings": sum(
            1 for c in cases if (c.get("reversal_postings") or 0) > 1
        ),
        "v11_cases": len(v11_cases),
        "v11_unexpected": len(unexpected),
        "v11_unexpected_cases": ";".join(c["case"] for c in unexpected),
        **safe,
    }
    outputs = {
        "trackb_cases": write_csv(
            raw / f"{key}-trackb-cases.csv",
            [
                {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in c.items()}
                for c in cases
            ],
        ),
        "v11_cases": write_json(raw / f"{key}-v11-cases.json", v11_cases),
    }
    return outputs, row


def cmd_replay(args, env) -> None:
    out = Path(args.out) / "replay"
    raw = RAW / "replay"
    m = Campaign(out, "stage10-replay", env, raw, args.resume)
    rows = []
    for seed in args.seeds:
        for auto in (True, False):
            status, row = m.run(
                "S10-REPLAY", f"auto_reverse={auto}", seed, {"stack": STACK},
                lambda k, s=seed, a=auto: replay_once(s, a, k, raw),
            )  # fmt: skip
            print(f"replay seed={seed} auto_reverse={auto}: {status}", flush=True)
            if row:
                rows.append({**row, "status": status})
    _finish(out, m, rows, ["condition"], ["v11_unexpected", "trackb_duplicate_reversal_postings"])


# -------------------------------------------------------------------------- output


def _finish(out: Path, m: Manifest, rows: list[dict], group: list[str], metrics: list[str]):
    if not rows:
        return
    for r in rows:
        r["group"] = "|".join(str(r.get(g)) for g in group)
    runs = write_csv(out / f"{m.experiment_id}-runs.csv", rows)
    completed = [r for r in rows if r.get("status") == "completed"]
    summary = {
        "experiment_id": m.experiment_id,
        "runs": len(rows),
        "completed": len(completed),
        "failed": len(rows) - len(completed),
        "by_group": aggregate(completed, "group", metrics),
    }
    conds = {r["condition"] for r in completed}
    if {"flood_undefended", "flood_defended"} <= conds:
        top = [r for r in completed if r.get("attack_rps") == FLOOD_RATES[-1]]
        summary["paired_defended_minus_undefended"] = {
            k: paired_diff(top, "flood_defended", "flood_undefended", k)
            for k in ("legit_check_success_during", "legit_check_p95_ms_during", "A3_accepted")
        }
    write_json(out / f"{m.experiment_id}-summary.json", summary)
    print(
        {"runs_csv": str(runs), "completed": len(completed), "failed": len(rows) - len(completed)}
    )


def main() -> None:
    p = argparse.ArgumentParser(description="Stage 10 integrated security experiments")
    p.add_argument(
        "command", choices=["flooding", "poisoning", "resilience", "replay", "environment"]
    )
    p.add_argument("--seeds", type=int, nargs="+", default=SEEDS)
    p.add_argument("--cases", nargs="+", default=None)
    p.add_argument("--out", default=str(OUT))
    p.add_argument("--resume", default=None, help="experiment id of an interrupted campaign")
    args = p.parse_args()
    env = {
        **environment(),
        "source_sha256": source_digest(),
        "source_digest_scope": "backend/aorts, intelligence, contracts (*.py)",
    }
    Path(args.out).mkdir(parents=True, exist_ok=True)
    write_json(Path(args.out) / f"environment-{args.command}.json", env)
    if args.command == "environment":
        print(json.dumps(env, indent=2))
        return
    {"flooding": cmd_flooding, "poisoning": cmd_poisoning, "resilience": cmd_resilience,
     "replay": cmd_replay}[args.command](args, env)  # fmt: skip


if __name__ == "__main__":
    main()
