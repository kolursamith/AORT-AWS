"""Stage 11 operator CLI for the running Docker system (synthetic mock environment only).

Run inside the ``aorts-loop`` container (it holds the service keys and the database URL):

    docker compose -p aorts-s11 -f infra/aorts/docker-compose.yml exec aorts-loop \
        python -m backend.aorts.integration.demo <command>

Commands
    traffic   --seconds 40 --rps 6 [--dest B C]   honest signed payments (demonstrated capacity)
    ambiguous --n 10 [--dest B]                  payments whose switch response is lost after
                                                 the beneficiary committed (genuine UNKNOWN)
    trace     TXN_ID                             ledger state, history, evidence, outbox
                                                 delivery and the v1.1 decisions for one payment
    decisions [--last 20]                        recent RECOVERY_DECISION_V11 audit rows
    summary                                      the dashboard's v1.1 security summary (control)
    export    --out DIR                          experiment rows as JSON + SHA-256 manifest
    wait-resolved TXN_ID [--timeout 180]         poll until the payment leaves AMBIGUOUS
    e2e       [--out FILE]                       the Stage 11 end-to-end check (below)
    replay-outbox                                operator procedure after a controller restart:
                                                 re-send the outbox (intake is idempotent)

No command moves money by itself: payments go through the remitter API, and every recovery
action is the integrated loop's (controller decision -> Track B execution gate -> check).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import uuid
from pathlib import Path

from sqlalchemy import text

TABLES = {
    "transactions": "SELECT * FROM transactions WHERE experiment_id = :e ORDER BY created_at",
    "transaction_events": (
        "SELECT te.* FROM transaction_events te JOIN transactions t USING (transaction_id) "
        "WHERE t.experiment_id = :e ORDER BY te.transaction_id, te.new_version"
    ),
    "evidence_records": (
        "SELECT ev.* FROM evidence_records ev JOIN transactions t USING (transaction_id) "
        "WHERE t.experiment_id = :e ORDER BY ev.created_at"
    ),
    "telemetry_windows": (
        "SELECT * FROM v11_telemetry_windows WHERE experiment_id = :e ORDER BY source_id, sequence"
    ),
    "decision_audit": (
        "SELECT * FROM audit_events WHERE action = 'RECOVERY_DECISION_V11' "
        "AND experiment_id = :e ORDER BY ts"
    ),
    "dead_letters": "SELECT * FROM v11_dead_letters ORDER BY created_at",
}


def _client():
    from backend.aorts.experiments.common import http_client

    return http_client()


def _db():
    from backend.aorts.app.config import get_settings
    from backend.aorts.app.db import Database

    settings = get_settings()
    return Database(settings.database_url.get_secret_value()), settings


def _pay(client, settings, dest: str, i: int, *, lost: bool, tag: str) -> dict:
    headers = {}
    if lost:
        headers = {
            "X-AORTS-Simulate": "lost-response",
            "X-AORTS-Admin-Token": settings.admin_token.get_secret_value(),
        }
    account = i % 10 + 1
    r = client.post(
        "remitter",
        "/api/v1/payments",
        {
            "sender_account_id": f"A1-CUST-{account:04d}",
            "beneficiary_account_id": f"{dest}-CUST-{account:04d}",
            "beneficiary_bank_id": dest,
            "amount_paise": 1_000 + i,
            "idempotency_key": f"{tag}-{uuid.uuid4().hex}",
        },
        as_identity="CHANNEL",
        headers=headers,
        timeout=10.0,
    )
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    txn = body.get("transaction") or {}
    return {"http": r.status_code, "transaction_id": txn.get("transaction_id"),
            "state": txn.get("state")}  # fmt: skip


def cmd_traffic(args) -> None:
    client, settings = _client()
    end, i, states = time.time() + args.seconds, 0, {}
    while time.time() < end:
        for dest in args.dest:
            out = _pay(client, settings, dest, i, lost=False, tag="demo-traffic")
            states[out["state"]] = states.get(out["state"], 0) + 1
            i += 1
        time.sleep(1.0 / args.rps)
    print(json.dumps({"payments": i, "states": states}))


def cmd_ambiguous(args) -> None:
    client, settings = _client()
    rows = [
        _pay(client, settings, args.dest, i, lost=True, tag="demo-ambiguous") for i in range(args.n)
    ]
    print(json.dumps(rows, indent=2))


def _rows(session, sql: str, **params) -> list[dict]:
    return [dict(r._mapping) for r in session.execute(text(sql), params)]


def cmd_trace(args) -> None:
    db, settings = _db()
    with db.session() as s:
        txn = _rows(s, "SELECT * FROM transactions WHERE transaction_id = :t", t=args.txn)
        history = _rows(
            s,
            "SELECT * FROM transaction_events WHERE transaction_id = :t ORDER BY new_version",
            t=args.txn,
        )
        evidence = _rows(
            s, "SELECT * FROM evidence_records WHERE transaction_id = :t ORDER BY created_at",
            t=args.txn,
        )  # fmt: skip
        outbox = _rows(
            s,
            "SELECT id, event_type, created_at FROM outbox_events "
            "WHERE aggregate_id = :t ORDER BY id",
            t=args.txn,
        )
        decisions = _rows(
            s,
            "SELECT ts, route_id, decision, reason, metadata FROM audit_events "
            "WHERE action = 'RECOVERY_DECISION_V11' AND experiment_id = :e "
            "AND metadata::text LIKE :t ORDER BY ts",
            e=settings.experiment_id,
            t=f"%{args.txn}%",
        )
    print(json.dumps({"transaction": txn, "history": history, "evidence": evidence,
                      "outbox": outbox, "decisions_naming_it": decisions},
                     indent=2, default=str))  # fmt: skip


def cmd_wait_resolved(args) -> None:
    db, _ = _db()
    deadline = time.time() + args.timeout
    state = None
    while time.time() < deadline:
        with db.session() as s:
            state = s.execute(
                text("SELECT state FROM transactions WHERE transaction_id = :t"), {"t": args.txn}
            ).scalar()
        if state not in ("AMBIGUOUS", "CHECK_IN_FLIGHT", "DEBITED"):
            break
        time.sleep(2)
    print(json.dumps({"transaction_id": args.txn, "state": state}))
    if state in ("AMBIGUOUS", "CHECK_IN_FLIGHT", "DEBITED"):
        sys.exit(2)


def cmd_decisions(args) -> None:
    db, settings = _db()
    with db.session() as s:
        rows = _rows(
            s,
            "SELECT ts, route_id, decision, reason, metadata FROM audit_events "
            "WHERE action = 'RECOVERY_DECISION_V11' AND experiment_id = :e "
            "ORDER BY ts DESC LIMIT :n",
            e=settings.experiment_id,
            n=args.last,
        )
    print(json.dumps(rows[::-1], indent=2, default=str))


def cmd_summary(args) -> None:
    client, settings = _client()
    r = client.request(
        "control",
        "GET",
        f"/api/v1/aorts/v11/summary?experiment_id={settings.experiment_id}",
        as_identity="LAB",
    )
    print(json.dumps(r.json(), indent=2, default=str))


def cmd_export(args) -> None:
    db, settings = _db()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    manifest = {"experiment_id": settings.experiment_id, "files": {}}
    with db.session() as s:
        for name, sql in TABLES.items():
            rows = _rows(s, sql, e=settings.experiment_id)
            path = out / f"{name}.json"
            path.write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
            manifest["files"][name] = {
                "rows": len(rows),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))


def _decisions_since(s, exp: str, since) -> list[dict]:
    return _rows(
        s,
        "SELECT ts, route_id, decision, reason, metadata FROM audit_events "
        "WHERE action = 'RECOVERY_DECISION_V11' AND experiment_id = :e AND ts > :since "
        "ORDER BY ts",
        e=exp,
        since=since,
    )


class _Traffic:
    """Honest signed payments to B and C at ``rps`` per route, sent concurrently."""

    def __init__(self, client, settings):
        import threading
        from concurrent.futures import ThreadPoolExecutor

        self.client, self.settings = client, settings
        self.pool = ThreadPoolExecutor(max_workers=24)
        self.stop_flag = threading.Event()
        self.thread = None
        self.sent = 0

    def _loop(self, rps: float, until: float) -> None:
        nxt = time.time()
        while not self.stop_flag.is_set() and time.time() < until:
            for dest in ("B", "C"):
                self.pool.submit(_pay, self.client, self.settings, dest, self.sent, lost=False,
                                 tag="e2e-honest")  # fmt: skip
                self.sent += 1
            nxt += 1.0 / rps
            time.sleep(max(0.0, nxt - time.time()))

    def run(self, rps: float, seconds: float) -> None:
        self._loop(rps, time.time() + seconds)

    def start(self, rps: float) -> None:
        import threading

        self.thread = threading.Thread(target=self._loop, args=(rps, float("inf")), daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_flag.set()
        if self.thread:
            self.thread.join(timeout=5)
        self.pool.shutdown(wait=True)


def cmd_e2e(args) -> None:
    """Stage 11 end-to-end check against the running stack; every step from stored evidence.

    Phase Z (zero budget): ambiguous payments created before the controller has demonstrated
    capacity must get no controller-authorised check (WARMING_UP / FAIL_CLOSED / NO_SAFE_BUDGET
    are honoured; nothing is dispatched under a controller decision id).
    Phase P (positive budget): after honest traffic, new ambiguous payments are resolved by
    status checks that a controller DECISION authorised and the execution gate admitted, with a
    verified signed STATUS_CHECK_RESPONSE and a closed ledger transaction. If the safe controller
    never selects a positive budget, that is reported as the result (exit code 3), not forced.
    """

    db, settings = _db()
    client, _ = _client()
    exp = settings.experiment_id
    report: dict = {"experiment_id": exp, "steps": {}}

    def now_db():
        with db.session() as s:
            return s.execute(text("SELECT now()")).scalar()

    # ---- phase Z: ambiguity before warm-up
    t_z = now_db()
    # The ambiguous backlog is created before any honest telemetry, as in the Stage 10 protocol:
    # lost confirmations arriving during the honest windows are timeouts that the estimator
    # cannot tell from congestion, so they restart its regime and discard the demonstrated
    # capacity (``--ambiguous-after-warmup`` reproduces that case; it ends with zero budget).
    zero = [
        _pay(client, settings, "B", i, lost=True, tag="e2e-zero") for i in range(args.ambiguous)
    ]
    time.sleep(args.zero_wait)
    with db.session() as s:
        dz = _decisions_since(s, exp, t_z)
    by_source: dict = {}
    for d in dz:
        meta = d["metadata"] or {}
        by_source[meta.get("source")] = by_source.get(meta.get("source"), 0) + 1
    controller_dispatch_z = sum(
        (d["metadata"] or {}).get("dispatched", 0)
        for d in dz
        if (d["metadata"] or {}).get("source") == "controller"
    )
    report["steps"]["zero_budget"] = {
        "ambiguous_created": [z["transaction_id"] for z in zero],
        "states": [z["state"] for z in zero],
        "decision_ticks_by_source": by_source,
        "outcomes": sorted({(d["metadata"] or {}).get("outcome") for d in dz} - {None}),
        "checks_dispatched_under_controller_decisions": controller_dispatch_z,
    }

    # ---- phase P: demonstrated capacity (the Stage 10 protocol: honest high load, then calm
    # load, so the estimator has served work above the current load), then ambiguity
    honest = _Traffic(client, settings)
    honest.run(args.warm_rps, args.warm)
    honest.start(args.rps)  # calm honest traffic keeps the telemetry windows informative
    t_p = now_db()
    if args.ambiguous_after_warmup:
        targets = [_pay(client, settings, "B", 100 + k, lost=True, tag="e2e-pos") for k in range(5)]
        ids = [t["transaction_id"] for t in targets]
    else:
        ids = [z["transaction_id"] for z in zero]
    deadline = time.time() + args.timeout
    states: dict = {}
    while time.time() < deadline:
        time.sleep(2.0)
        with db.session() as s:
            states = dict(
                s.execute(
                    text("SELECT transaction_id, state FROM transactions "
                         "WHERE transaction_id = ANY(:t)"),
                    {"t": ids},
                ).all()
            )  # fmt: skip
        if all(v not in ("AMBIGUOUS", "CHECK_IN_FLIGHT", "DEBITED") for v in states.values()):
            break
    honest.stop()
    report["steps"]["honest_payments"] = honest.sent
    with db.session() as s:
        dp = _decisions_since(s, exp, t_p)
        history = _rows(
            s,
            "SELECT transaction_id, event_type, previous_state, new_state, new_version, actor, "
            "metadata FROM transaction_events WHERE transaction_id = ANY(:t) "
            "ORDER BY transaction_id, new_version",
            t=ids,
        )
        evidence = _rows(
            s,
            "SELECT transaction_id, evidence_kind, reported_status, verified, check_id, key_id "
            "FROM evidence_records WHERE transaction_id = ANY(:t) ORDER BY created_at",
            t=ids,
        )
        windows = _rows(
            s,
            "SELECT source_id, verdict, count(*) AS n FROM v11_telemetry_windows "
            "WHERE experiment_id = :e GROUP BY 1, 2 ORDER BY 1, 2",
            e=exp,
        )
        ledger = s.execute(text("SELECT count(*) FROM ledger_entries")).scalar()
    positive = [
        d for d in dp
        if (d["metadata"] or {}).get("source") == "controller"
        and (d["metadata"] or {}).get("dispatched", 0) > 0
    ]  # fmt: skip
    verified_checks = [
        e for e in evidence if e["evidence_kind"] == "STATUS_CHECK_RESPONSE" and e["verified"]
    ]
    resolved = [t for t, v in states.items() if v not in ("AMBIGUOUS", "CHECK_IN_FLIGHT")]
    reasons: dict = {}
    for d in dz + dp:
        meta = d["metadata"] or {}
        key = f"{meta.get('outcome')}:{meta.get('controller_reason')}"
        reasons[key] = reasons.get(key, 0) + 1
    report["controller_reasons"] = reasons
    report["steps"]["positive_budget"] = {
        "ambiguous_created": ids,
        "final_states": states,
        "controller_decisions_with_dispatch": len(positive),
        "decision_example": positive[0] if positive else None,
        "verified_status_check_evidence": len(verified_checks),
        "resolved": len(resolved),
        "history": history,
        "evidence": evidence,
    }
    report["telemetry_windows_by_verdict"] = windows
    report["ledger_entries"] = ledger
    zero_ok = controller_dispatch_z == 0
    pos_ok = bool(positive) and bool(verified_checks) and len(resolved) == len(ids)
    report["verdict"] = {"zero_budget_honoured": zero_ok, "positive_budget_resolved": pos_ok}
    text_out = json.dumps(report, indent=2, default=str)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text_out, encoding="utf-8")
    print(json.dumps(report["verdict"]))
    print(json.dumps({k: v for k, v in report["steps"]["positive_budget"].items()
                      if k in ("final_states", "controller_decisions_with_dispatch",
                               "verified_status_check_evidence")}, default=str))  # fmt: skip
    if not zero_ok:
        sys.exit(2)
    if not pos_ok:
        sys.exit(3)


def cmd_replay_outbox(args) -> None:
    from backend.aorts.integration.v11 import Delivery

    db, settings = _db()
    Delivery(db, None, token="", experiment_id=settings.experiment_id, policy_id="").replay_events()
    print(json.dumps({"experiment_id": settings.experiment_id, "outbox_cursor": "reset"}))


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description="AORTS Stage 11 operator CLI (synthetic only)")
    sub = p.add_subparsers(dest="command", required=True)
    t = sub.add_parser("traffic")
    t.add_argument("--seconds", type=float, default=40.0)
    t.add_argument("--rps", type=float, default=6.0)
    t.add_argument("--dest", nargs="+", default=["B", "C"])
    a = sub.add_parser("ambiguous")
    a.add_argument("--n", type=int, default=10)
    a.add_argument("--dest", default="B")
    tr = sub.add_parser("trace")
    tr.add_argument("txn")
    w = sub.add_parser("wait-resolved")
    w.add_argument("txn")
    w.add_argument("--timeout", type=float, default=180.0)
    d = sub.add_parser("decisions")
    d.add_argument("--last", type=int, default=20)
    sub.add_parser("summary")
    e = sub.add_parser("export")
    e.add_argument("--out", default="/app/results/stage11-export")
    sub.add_parser("replay-outbox")
    x = sub.add_parser("e2e")
    x.add_argument("--out", default="/app/results/stage11-e2e.json")
    x.add_argument("--zero-wait", type=float, default=8.0)
    x.add_argument("--ambiguous", type=int, default=5)
    x.add_argument("--ambiguous-after-warmup", action="store_true")
    x.add_argument("--warm", type=float, default=40.0)
    x.add_argument("--warm-rps", type=float, default=12.0, help="honest payments/s per route")
    x.add_argument("--rps", type=float, default=3.0, help="calm honest payments/s per route")
    x.add_argument("--timeout", type=float, default=180.0)
    args = p.parse_args(argv)
    {
        "traffic": cmd_traffic,
        "ambiguous": cmd_ambiguous,
        "trace": cmd_trace,
        "wait-resolved": cmd_wait_resolved,
        "decisions": cmd_decisions,
        "summary": cmd_summary,
        "export": cmd_export,
        "replay-outbox": cmd_replay_outbox,
        "e2e": cmd_e2e,
    }[args.command](args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
