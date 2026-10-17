"""Read models for the dashboard: payment KPIs, security counters, decisions, audit trail.

Everything here is MEASURED from the database or the switch's counters; predictions
from the controller are returned in separate, clearly named fields.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text

from backend.aorts.app.metrics import percentile
from backend.aorts.banking.ledger.models import (
    AuditEvent,
    OutboxEvent,
    TelemetryObservation,
    Transaction,
)

SECURITY_ACTIONS = (
    "AUTH_FAILURE",
    "STATUS_CHECK_REJECTED",
    "TELEMETRY_REJECTED",
    "TELEMETRY_ANOMALY",
    "EVIDENCE_REJECTED",
    "DUPLICATE_EVENT_REJECTED",
    "REVERSAL_REJECTED",
    "REVERSAL_DUPLICATE",
    "EVIDENCE_DUPLICATE",
    "STATE_EVENT_REJECTED",
    "PAYMENT_REJECTED",
)


class MetricsService:
    def __init__(self, ctx, ledger):
        self.ctx = ctx
        self.ledger = ledger

    def _switch(self, path: str) -> dict:
        try:
            r = self.ctx.client.get("switch", path, as_identity="CONTROLLER", timeout=2.0)
            return r.json() if r.status_code == 200 else {"error": f"HTTP {r.status_code}"}
        except Exception as exc:  # noqa: BLE001
            return {"error": type(exc).__name__}

    def summary(self) -> dict:
        db = self.ctx.db
        with db.session() as s:
            by_state = dict(
                s.execute(select(Transaction.state, func.count()).group_by(Transaction.state)).all()
            )
            by_outcome = dict(
                s.execute(
                    select(Transaction.financial_outcome, func.count()).group_by(
                        Transaction.financial_outcome
                    )
                ).all()
            )
            by_route = self._route_rows(s)
            resolved = s.execute(
                select(Transaction.ambiguous_since, Transaction.resolved_at)
                .where(
                    Transaction.ambiguous_since.is_not(None), Transaction.resolved_at.is_not(None)
                )
                .order_by(Transaction.resolved_at.desc())
                .limit(5000)
            ).all()
            audit_counts = s.execute(
                select(AuditEvent.action, AuditEvent.decision, func.count()).group_by(
                    AuditEvent.action, AuditEvent.decision
                )
            ).all()
            telemetry = dict(
                s.execute(
                    select(TelemetryObservation.verdict, func.count()).group_by(
                        TelemetryObservation.verdict
                    )
                ).all()
            )
            outbox_pending = s.execute(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.published_at.is_(None))
            ).scalar()
            outbox_dead = s.execute(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.dead_lettered.is_(True))
            ).scalar()
            checks = s.execute(
                select(func.avg(Transaction.checks_used), func.sum(Transaction.checks_used)).where(
                    Transaction.ambiguous_since.is_not(None)
                )
            ).one()
            kpi = (
                s.execute(
                    text(
                        "SELECT "
                        " count(*) FILTER (WHERE ambiguous_since IS NOT NULL AND (resolved_at IS NOT NULL OR deadline_at <= now()))"
                        "   AS deadline_population,"
                        " count(*) FILTER (WHERE ambiguous_since IS NOT NULL AND resolved_at IS NOT NULL"
                        "   AND resolved_at <= deadline_at) AS resolved_in_deadline,"
                        " count(*) FILTER (WHERE ambiguous_since IS NOT NULL AND resolved_at IS NOT NULL) AS ambiguous_resolved,"
                        " count(*) FILTER (WHERE created_at >= now() - interval '60 seconds') AS created_60s,"
                        " count(*) FILTER (WHERE created_at >= now() - interval '60 seconds' AND financial_outcome='SUCCESS'"
                        "   AND resolved_at - created_at <= interval '2 seconds') AS good_60s"
                        " FROM transactions"
                    )
                )
                .mappings()
                .one()
            )
        res_s = [(b - a).total_seconds() for a, b in resolved]
        audit = {f"{a}:{d}": int(n) for a, d, n in audit_counts}

        def a(action, decision=None):
            return sum(
                v
                for k, v in audit.items()
                if k.startswith(action + ":") and (decision is None or k.endswith(":" + decision))
            )

        switch = self._switch("/api/v1/metrics")
        counters = switch.get("counters", {}) if isinstance(switch, dict) else {}
        total = sum(by_state.values())
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "label": "SYNTHETIC MOCK ENVIRONMENT - measured values",
            "payments": {
                "total": total,
                "by_state": by_state,
                "by_outcome": by_outcome,
                "successful": by_outcome.get("SUCCESS", 0),
                "failed_or_reversed": by_outcome.get("FAILURE", 0) + by_outcome.get("REVERSED", 0),
                "ambiguous_backlog": by_state.get("AMBIGUOUS", 0)
                + by_state.get("CHECK_IN_FLIGHT", 0),
                "escalated": by_state.get("HANDOFF_UDIR", 0),
                "quarantined": by_state.get("QUARANTINED", 0),
                "by_route": by_route,
            },
            "resolution_latency_s": {
                "samples": len(res_s),
                "p50": _r(percentile(res_s, 0.5)),
                "p95": _r(percentile(res_s, 0.95)),
            },
            "kpis": {
                # Simulated-deadline compliance: ambiguous txns resolved before their (simulated T+1) deadline,
                # over ambiguous txns that are resolved or already past the deadline.
                "deadline_compliance": _ratio(
                    kpi["resolved_in_deadline"], kpi["deadline_population"]
                ),
                "deadline_population": int(kpi["deadline_population"]),
                # Goodput: payments created in the last 60 s that SUCCEEDED within the 2 s objective, per second.
                "payment_goodput_per_s_60s": round(int(kpi["good_60s"]) / 60.0, 3),
                "payments_created_60s": int(kpi["created_60s"]),
                # Status-check efficiency: authoritative outcomes obtained per status check used.
                "outcomes_per_status_check": _ratio(kpi["ambiguous_resolved"], int(checks[1] or 0)),
                "switch_timeout_rate": _ratio(
                    _sum(counters, "pay_timeout"), _sum(counters, "pay_received")
                ),
            },
            "status_checks": {
                "avg_checks_per_ambiguous_txn": _r(float(checks[0]))
                if checks[0] is not None
                else None,
                "total_checks_used": int(checks[1] or 0),
                "received": _sum(counters, "check_received"),
                "admitted": _sum(counters, "check_admitted"),
                "rejected": _sum(counters, "check_rejected"),
                "forwarded_downstream": _sum(counters, "check_forwarded"),
                "rejected_by_reason": _by_label(counters, "check_rejected", "reason"),
                "admitted_by_bank": _by_label(counters, "check_admitted", "bank"),
                "rejected_by_bank": _by_label(counters, "check_rejected", "bank"),
                "core_overloaded": _sum(counters, "core_overloaded"),
            },
            "security": {
                "auth_failures": a("AUTH_FAILURE"),
                "telemetry": telemetry,
                "duplicate_events_rejected": a("DUPLICATE_EVENT_REJECTED")
                + a("EVIDENCE_DUPLICATE"),
                "duplicate_financial_operations_prevented": a("REVERSAL_DUPLICATE")
                + a("REVERSAL_REJECTED"),
                "invalid_transitions_or_stale_prevented": a("EVIDENCE_REJECTED")
                + a("STATE_EVENT_REJECTED"),
                "payments_rejected": a("PAYMENT_REJECTED"),
                "rate_limit_audit_rows_sampled": a("STATUS_CHECK_REJECTED"),
            },
            "outbox": {"pending": outbox_pending, "dead_lettered": outbox_dead},
            "switch": {
                k: switch.get(k)
                for k in ("routes", "capacity", "core", "defenses", "uptime_s")
                if isinstance(switch, dict)
            },
            "quotas": {
                b: self._switch(f"/api/v1/security/banks/{b}/quota") for b in ("A1", "A2", "A3")
            },
        }

    @staticmethod
    def _route_rows(s) -> list[dict]:
        rows = (
            s.execute(
                text(
                    "SELECT route_id, count(*) AS total, "
                    "sum(CASE WHEN state IN ('AMBIGUOUS','CHECK_IN_FLIGHT') THEN 1 ELSE 0 END) AS ambiguous, "
                    "sum(CASE WHEN financial_outcome='SUCCESS' THEN 1 ELSE 0 END) AS success "
                    "FROM transactions GROUP BY route_id ORDER BY route_id"
                )
            )
            .mappings()
            .all()
        )
        return [dict(r) for r in rows]

    def timeseries(self, seconds: int = 120) -> dict:
        since = datetime.now(UTC) - timedelta(seconds=seconds)
        with self.ctx.db.session() as s:
            created = (
                s.execute(
                    text(
                        "SELECT extract(epoch from date_trunc('second', created_at))::bigint AS t, route_id, count(*) AS n "
                        "FROM transactions WHERE created_at >= :since GROUP BY 1, 2 ORDER BY 1"
                    ),
                    {"since": since},
                )
                .mappings()
                .all()
            )
            ambiguous = (
                s.execute(
                    text(
                        "SELECT extract(epoch from date_trunc('second', ambiguous_since))::bigint AS t, count(*) AS n "
                        "FROM transactions WHERE ambiguous_since >= :since GROUP BY 1 ORDER BY 1"
                    ),
                    {"since": since},
                )
                .mappings()
                .all()
            )
            resolved = (
                s.execute(
                    text(
                        "SELECT extract(epoch from date_trunc('second', resolved_at))::bigint AS t, count(*) AS n "
                        "FROM transactions WHERE resolved_at >= :since GROUP BY 1 ORDER BY 1"
                    ),
                    {"since": since},
                )
                .mappings()
                .all()
            )
        switch = self._switch(f"/api/v1/metrics/timeseries?seconds={seconds}")
        return {
            "payments_created": [dict(r) for r in created],
            "became_ambiguous": [dict(r) for r in ambiguous],
            "resolved": [dict(r) for r in resolved],
            "switch_series": switch.get("series", []),
        }

    def decisions(self, limit: int = 50) -> list[dict]:
        with self.ctx.db.session() as s:
            rows = s.execute(
                select(AuditEvent)
                .where(AuditEvent.action == "RECOVERY_DECISION")
                .order_by(AuditEvent.seq.desc())
                .limit(limit)
            ).scalars()
            return [
                {
                    "seq": r.seq,
                    "ts": r.ts.isoformat(),
                    "decision": r.decision,
                    "reason": r.reason,
                    **(r.metadata_json or {}),
                }
                for r in rows
            ]

    def audit(
        self,
        *,
        limit: int = 100,
        before_seq: int | None = None,
        action: str | None = None,
        transaction_id: str | None = None,
        security_only: bool = False,
    ) -> dict:
        with self.ctx.db.session() as s:
            stmt = select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(limit)
            if before_seq:
                stmt = stmt.where(AuditEvent.seq < before_seq)
            if action:
                stmt = stmt.where(AuditEvent.action == action)
            if transaction_id:
                stmt = stmt.where(AuditEvent.transaction_id == transaction_id)
            if security_only:
                stmt = stmt.where(AuditEvent.action.in_(SECURITY_ACTIONS))
            rows = s.execute(stmt).scalars().all()
        items = [
            {
                "seq": r.seq,
                "event_id": r.event_id,
                "ts": r.ts.isoformat(),
                "actor": r.actor,
                "action": r.action,
                "decision": r.decision,
                "reason": r.reason,
                "transaction_id": r.transaction_id,
                "route_id": r.route_id,
                "previous_version": r.previous_version,
                "new_version": r.new_version,
                "experiment_id": r.experiment_id,
                "metadata": r.metadata_json,
                "hash": r.hash[:16],
            }
            for r in rows
        ]
        return {
            "items": items,
            "next_before_seq": items[-1]["seq"] if len(items) == limit else None,
        }


def _r(x):
    return None if x is None else round(x, 3)


def _ratio(num, den):
    return round(int(num) / int(den), 4) if den else None


def _sum(counters: dict, name: str) -> int:
    return int(sum(v for k, v in counters.items() if k == name or k.startswith(name + "{")))


def _by_label(counters: dict, name: str, label: str) -> dict:
    out: dict[str, int] = {}
    for k, v in counters.items():
        if not k.startswith(name + "{"):
            continue
        labels = dict(p.split("=", 1) for p in k[len(name) + 1 : -1].split(","))
        out[labels.get(label, "?")] = out.get(labels.get(label, "?"), 0) + int(v)
    return out
