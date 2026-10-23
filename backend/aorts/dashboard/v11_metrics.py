"""AORTS v1.1 security view for the dashboard (pure functions, no Streamlit).

Every number comes from a recorded source and nothing is simulated here:

* the ledger tables (transactions, transaction events, financial operations),
* the evidence records the remitter stored for each signed switch response (Stage 09R),
* the telemetry windows and the validator verdicts, and the delivery dead letters,
* the hash-chained audit log: one ``RECOVERY_DECISION_V11`` per route and tick, written by the
  dispatcher, which says whether checks were genuine AORTS decisions, Track B's labelled
  fallback or an honoured fail-closed; plus the replay and authentication rejections,
* the switch's per-bank counters (passed in from the control service's switch metrics),
* the Stage 10 evidence package (runs CSVs) for detection, containment and recovery times.

The values are only as good as those records: windows that were never produced, or audit rows
that were sampled away, are absent and are never filled in.
"""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from pathlib import Path

from sqlalchemy import text

UNRESOLVED = ("DEBITED", "AMBIGUOUS", "CHECK_IN_FLIGHT")
REPLAY_ACTIONS = (
    "AUTH_FAILURE",
    "REVERSAL_DUPLICATE",
    "EVIDENCE_DUPLICATE",
    "FOREIGN_TRANSACTION_ID",
)
SETTLING_EVENTS = ("PAYMENT_SUCCEEDED", "PAYMENT_FAILED", "PAYMENT_CLOSED", "REVERSAL_COMPLETED")


def _rows(session, sql: str, **params):
    return session.execute(text(sql), params).all()


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return round(v[min(len(v) - 1, max(0, int(round(q * (len(v) - 1)))))], 2)


def decisions(session, experiment_id: str | None = None, limit: int = 2000) -> dict:
    """Recovery ticks by route and source: genuine AORTS vs fallback vs honoured fail-closed."""

    rows = _rows(
        session,
        "SELECT route_id, metadata, ts FROM audit_events WHERE action = 'RECOVERY_DECISION_V11' "
        "AND (CAST(:exp AS text) IS NULL OR experiment_id = :exp) ORDER BY seq DESC LIMIT :n",
        exp=experiment_id,
        n=limit,
    )
    by_route: dict = defaultdict(lambda: {"ticks": Counter(), "dispatched": Counter(),
                                          "outcomes": Counter(), "gate_refused": Counter()})  # fmt: skip
    latest = {}
    for route, meta, ts in rows:
        meta = meta or {}
        src = meta.get("source", "unknown")
        r = by_route[route]
        r["ticks"][src] += 1
        r["dispatched"][src] += int(meta.get("dispatched") or 0)
        r["outcomes"][meta.get("outcome", "unknown")] += 1
        for reason in (meta.get("gate_refused") or {}).values():
            r["gate_refused"][reason] += 1
        latest.setdefault(route, {"at": ts, **{k: meta.get(k) for k in ("outcome", "source",
                                  "decision_id", "dispatched", "admitted")}})  # fmt: skip
    return {
        "routes": {k: {kk: dict(vv) for kk, vv in v.items()} for k, v in sorted(by_route.items())},
        "latest": latest,
        "ticks_read": len(rows),
    }


def telemetry(session, experiment_id: str | None = None, last: int = 300) -> dict:
    """Validator verdicts and trust per route/source over the latest windows."""

    rows = _rows(
        session,
        "SELECT route_id, source_id, sequence, verdict, confidence, cross_check, reasons, "
        "delivered_at IS NOT NULL AND last_error IS NULL AS acknowledged, window_end "
        "FROM v11_telemetry_windows WHERE (CAST(:exp AS text) IS NULL OR experiment_id = :exp) "
        "ORDER BY id DESC LIMIT :n",
        exp=experiment_id,
        n=last,
    )
    out: dict = defaultdict(lambda: {"verdicts": Counter(), "windows": 0, "acknowledged": 0})
    latest = {}
    for route, source, seq, verdict, conf, cross, reasons, acked, end in rows:
        r = out[route]
        r["windows"] += 1
        r["verdicts"][verdict] += 1
        r["acknowledged"] += int(bool(acked))
        r["source_id"] = source
        latest.setdefault(route, {"sequence": seq, "verdict": verdict, "confidence": conf,
                                  "cross_check": cross, "window_end": end,
                                  "reasons": list(reasons or [])[:5]})  # fmt: skip
    for route, r in out.items():
        r["verdicts"] = dict(r["verdicts"])
        r["suspicious_share"] = round(
            1 - r["verdicts"].get("TRUSTED", 0) / r["windows"], 4
        ) if r["windows"] else None  # fmt: skip
        r["latest"] = latest.get(route)
    return dict(sorted(out.items()))


def checks_and_backlog(session, experiment_id: str | None = None) -> dict:
    exp = {"exp": experiment_id}
    states = dict(
        _rows(
            session,
            "SELECT state, count(*) FROM transactions t WHERE (CAST(:exp AS text) IS NULL OR t.experiment_id = :exp) GROUP BY 1",
            **exp,
        )
    )
    rejected = Counter()
    for (fact,) in _rows(
        session,
        "SELECT e.metadata->'check_fact' FROM transaction_events e JOIN transactions t "
        "USING (transaction_id) WHERE e.metadata->'check_fact'->>'kind' = 'REJECTED' AND (CAST(:exp AS text) IS NULL OR t.experiment_id = :exp)",
        **exp,
    ):
        rejected[(fact or {}).get("trackb_code", "UNKNOWN")] += 1
    return {
        "states": states,
        "unresolved": sum(states.get(s, 0) for s in UNRESOLVED),
        "in_flight_checks": states.get("CHECK_IN_FLIGHT", 0),
        "rejected_checks_by_code": dict(rejected),
    }


def evidence(session, experiment_id: str | None = None) -> dict:
    rows = _rows(
        session,
        "SELECT e.evidence_kind, e.verified, count(*) FROM evidence_records e JOIN transactions t "
        "USING (transaction_id) WHERE (CAST(:exp AS text) IS NULL OR t.experiment_id = :exp) GROUP BY 1, 2",
        exp=experiment_id,
    )
    out: dict = defaultdict(lambda: {"verified": 0, "unverified": 0})
    for kind, verified, n in rows:
        out[kind]["verified" if verified else "unverified"] += n
    lat = [
        float(v)
        for (v,) in _rows(
            session,
            "SELECT e.latency_ms FROM evidence_records e JOIN transactions t USING (transaction_id) "
            "WHERE e.evidence_kind = 'ORIGINAL_PAYMENT_RESPONSE' AND e.latency_ms IS NOT NULL AND "
            "(CAST(:exp AS text) IS NULL OR t.experiment_id = :exp) "
            "ORDER BY e.created_at DESC LIMIT 2000",
            exp=experiment_id,
        )
    ]
    return {
        "by_kind": dict(out),
        "payment_latency_ms": {"p50": _pct(lat, 0.5), "p95": _pct(lat, 0.95),
                               "p99": _pct(lat, 0.99), "samples": len(lat)},
    }  # fmt: skip


def replay_rejections(session, experiment_id: str | None = None) -> dict:
    audit = dict(
        _rows(
            session,
            "SELECT action, count(*) FROM audit_events WHERE action = ANY(:a) "
            "AND (CAST(:exp AS text) IS NULL OR experiment_id = :exp OR experiment_id IS NULL) "
            "GROUP BY 1",
            a=list(REPLAY_ACTIONS),
            exp=experiment_id,
        )
    )
    dead = dict(_rows(session, "SELECT error_code, count(*) FROM v11_dead_letters GROUP BY 1"))
    return {"audit": audit, "v11_dead_letters": dead}


def safety(session, ledger_ok: bool | None = None) -> dict:
    q = lambda sql: session.execute(text(sql)).scalar()  # noqa: E731
    return {
        "ledger_ok": ledger_ok,
        "duplicate_check_starts": q(
            "SELECT count(*) FROM (SELECT transaction_id, previous_version FROM transaction_events "
            "WHERE event_type='CHECK_STARTED' GROUP BY 1, 2 HAVING count(*) > 1) d"
        ),
        "multiple_reversals": q(
            "SELECT count(*) FROM (SELECT transaction_id FROM financial_operations "
            "WHERE operation_type='REVERSAL' GROUP BY 1 HAVING count(*) > 1) d"
        ),
        "multiple_debits": q(
            "SELECT count(*) FROM (SELECT transaction_id FROM financial_operations "
            "WHERE operation_type='DEBIT' GROUP BY 1 HAVING count(*) > 1) d"
        ),
        "unverified_evidence_applied": q(
            "SELECT count(*) FROM transaction_events te JOIN evidence_records er "
            "ON er.evidence_id = te.metadata->>'evidence_id' WHERE NOT er.verified AND "
            "te.event_type IN ('PAYMENT_SUCCEEDED','PAYMENT_FAILED','PAYMENT_CLOSED','REVERSAL_COMPLETED')"
        ),
    }


def per_bank_checks(switch_counters: dict) -> dict:
    """Per-bank status traffic from the switch's own counters (``check_received{bank=A1}`` ...)."""

    out: dict = defaultdict(lambda: {"received": 0, "admitted": 0, "rejected": 0,
                                     "rejected_by_reason": Counter()})  # fmt: skip
    for key, value in (switch_counters or {}).items():
        name, _, labels = key.partition("{")
        lab = dict(p.split("=", 1) for p in labels.rstrip("}").split(",") if "=" in p)
        bank = lab.get("bank")
        if not bank:
            continue
        if name == "check_received":
            out[bank]["received"] += value
        elif name == "check_admitted":
            out[bank]["admitted"] += value
        elif name == "check_rejected":
            out[bank]["rejected"] += value
            out[bank]["rejected_by_reason"][lab.get("reason", "?")] += value
    return {
        b: {**v, "rejected_by_reason": dict(v["rejected_by_reason"])}
        for b, v in sorted(out.items())
    }


def live_summary(session, *, experiment_id=None, switch_counters=None, ledger_ok=None) -> dict:
    return {
        "label": "AORTS v1.1: measured from the ledger, evidence records, telemetry windows, "
        "audit log and switch counters (synthetic mock environment)",
        "decisions": decisions(session, experiment_id),
        "telemetry": telemetry(session, experiment_id),
        "checks_and_backlog": checks_and_backlog(session, experiment_id),
        "evidence": evidence(session, experiment_id),
        "replay_rejections": replay_rejections(session, experiment_id),
        "per_bank_checks": per_bank_checks(switch_counters or {}),
        "financial_safety": safety(session, ledger_ok),
    }


def stage10_runs(base: Path) -> dict[str, list[dict]]:
    """Newest runs CSV of each Stage 10 experiment (as written; nothing recomputed)."""

    out = {}
    for kind in ("flooding", "poisoning", "resilience", "replay"):
        files = sorted((base / kind).glob("*-runs.csv")) if (base / kind).exists() else []
        if files:
            with files[-1].open(encoding="utf-8") as f:
                out[kind] = list(csv.DictReader(f))
    return out
