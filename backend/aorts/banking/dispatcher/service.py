"""Recovery dispatcher: turns controller recommendations into safe, bounded status checks.

One tick (default every second):

1. escalate AMBIGUOUS transactions past their (simulated T+1) deadline -> HANDOFF_UDIR;
2. collect eligible AMBIGUOUS candidates and per-route state (backlog, switch-side
   measurements, twin telemetry + integrity verdict);
3. ask the controller (Track A or mock) for a decision; fall back if it fails;
4. clamp each route budget with :class:`BudgetGuard`;
5. for each selected transaction: re-check eligibility, move it to CHECK_IN_FLIGHT
   with a version check, enqueue a :class:`DispatchJob`;
6. workers execute signed /check-status calls and the ledger applies the answer.

Run stand-alone: ``python -m backend.aorts.banking.dispatcher.service``.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from backend.aorts.adapters.controller_client import ResilientController, make_controller
from backend.aorts.adapters.twin_state import VersionedStore, make_store
from backend.aorts.app.dependencies import ServiceContext
from backend.aorts.app.logging_config import log_event
from backend.aorts.banking.ledger.invariants import SafetyViolation
from backend.aorts.banking.ledger.models import Transaction
from backend.aorts.banking.remitter.service import RemitterService
from backend.aorts.contracts.enums import Decision, TxnState
from backend.aorts.contracts.events import DispatchJob
from backend.aorts.contracts.requests import RecoveryDecisionRequest
from backend.aorts.recovery.gates import PreDispatchGate
from backend.aorts.security.audit import make_record
from backend.aorts.security.budget_guard import BudgetGuard

logger = logging.getLogger("aorts.dispatcher")


def make_queue(settings):
    if settings.queue_backend == "sqs":
        from backend.aorts.banking.dispatcher.sqs_adapter import SqsQueue

        return SqsQueue(settings.sqs_queue_url, settings.aws_region)
    from backend.aorts.banking.dispatcher.local_adapter import DuplicatingQueue, LocalQueue

    if settings.dispatch_duplicate_job_prob > 0:  # experiment knob: simulate at-least-once delivery
        return DuplicatingQueue(settings.dispatch_duplicate_job_prob, settings.random_seed)
    return LocalQueue()


class Dispatcher:
    def __init__(
        self,
        ctx: ServiceContext,
        remitter: RemitterService,
        *,
        controller: ResilientController | None = None,
        twin: VersionedStore | None = None,
        queue=None,
        guard: BudgetGuard | None = None,
        v11=None,
        v11_watermark=None,
    ):
        s = ctx.settings
        self.ctx = ctx
        # Optional v1.1 controller client (controller_client_v11.V11ControllerClient). When set,
        # every tick uses the v1.1 protocol and its execution gate instead of the /v1 path.
        self.v11 = v11
        # Optional route_id -> telemetry watermark (integration.v11.delivered_watermark): the
        # controller must have applied what Track B knows it delivered, or it fails closed.
        self.v11_watermark = v11_watermark
        self.remitter = remitter
        self.ledger = remitter.ledger
        self.twin = twin or make_store(s, ctx.db)
        self.controller = controller or make_controller(s, db=ctx.db, store=self.twin)
        self.queue = queue or make_queue(s)
        self.guard = guard or BudgetGuard(
            hard_cap=s.max_budget_per_route,
            fallback_budget=s.fallback_budget_per_route,
            max_telemetry_age_s=s.telemetry_max_age_seconds,
        )
        self.gate = PreDispatchGate(
            self.ledger,
            max_inflight_per_route=s.max_inflight_checks_per_route,
            primary_reserve_fraction=s.switch_primary_reserve_fraction,
        )
        self.last_decision: dict | None = None
        self.run_id: str | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ runtime strategy switching
    def _maybe_switch_controller(self) -> None:
        """Experiments set ``runtime_config['controller'] = {mode, run_id}``; a new run_id builds a FRESH
        controller (no state carried between runs) and fresh gate statistics."""
        cfg = self.ctx.runtime.get("controller")
        if not cfg or cfg.get("run_id") == self.run_id:
            return
        settings = self.ctx.settings.model_copy(update={"controller_mode": cfg["mode"]})
        self.controller = make_controller(settings, db=self.ctx.db, store=self.twin)
        self.gate.stats.clear()
        self.run_id = cfg.get("run_id")
        if hasattr(self.queue, "duplicates"):
            import random

            self.queue.duplicates = 0
            self.queue.rng = random.Random(
                f"dup|{cfg.get('seed', 0)}"
            )  # same duplication stream per seed
        log_event(logger, "controller switched", mode=cfg["mode"], run_id=self.run_id)

    def _publish_stats(self) -> None:
        from backend.aorts.adapters.twin_state import update_with_retry

        stats = {
            "run_id": self.run_id,
            "controller": self.controller.name,
            "gate": dict(self.gate.stats),
            "duplicate_jobs_injected": getattr(self.queue, "duplicates", 0),
            "stale_jobs_dropped": int(
                sum(
                    v
                    for k, v in self.ctx.metrics.counters.items()
                    if k.startswith("dispatch_stale_jobs")
                )
            ),
        }
        try:
            update_with_retry(self.twin, "dispatcher:stats", lambda _: stats)
        except Exception:  # noqa: BLE001 - stats publishing is best effort
            logging.getLogger(__name__).debug("best-effort step failed", exc_info=True)

    # ------------------------------------------------------------------ inputs
    def _measured(self) -> dict[str, dict]:
        try:
            r = self.ctx.client.get(
                "switch", "/api/v1/metrics/routes", as_identity="DISPATCHER", timeout=1.0
            )
            return r.json().get("routes", {}) if r.status_code == 200 else {}
        except Exception:  # noqa: BLE001 - measurements are optional inputs
            return {}

    def _candidates(self, now: datetime, limit: int = 500) -> list[Transaction]:
        with self.ctx.db.session() as s:
            return list(
                s.execute(
                    select(Transaction)
                    .where(
                        Transaction.state == TxnState.AMBIGUOUS.value,
                        (Transaction.next_eligible_at.is_(None))
                        | (Transaction.next_eligible_at <= now),
                        Transaction.checks_used < self.ledger.max_checks,
                        Transaction.deadline_at > now,
                    )
                    .order_by(Transaction.ambiguous_since)
                    .limit(limit)
                ).scalars()
            )

    def inflight_by_route(self) -> dict[str, int]:
        with self.ctx.db.session() as s:
            rows = s.execute(
                select(Transaction.route_id, func.count())
                .where(Transaction.state == TxnState.CHECK_IN_FLIGHT.value)
                .group_by(Transaction.route_id)
            ).all()
        return {r: int(n) for r, n in rows}

    def backlog_by_route(self) -> dict[str, int]:
        with self.ctx.db.session() as s:
            rows = s.execute(
                select(Transaction.route_id, func.count())
                .where(
                    Transaction.state.in_(
                        [TxnState.AMBIGUOUS.value, TxnState.CHECK_IN_FLIGHT.value]
                    )
                )
                .group_by(Transaction.route_id)
            ).all()
        return {r: int(n) for r, n in rows}

    def escalate_expired(self, now: datetime) -> int:
        with self.ctx.db.session() as s:
            expired = s.execute(
                select(Transaction.transaction_id, Transaction.version).where(
                    Transaction.state == TxnState.AMBIGUOUS.value, Transaction.deadline_at <= now
                )
            ).all()
            stuck = s.execute(
                select(Transaction.transaction_id, Transaction.version).where(
                    Transaction.state == TxnState.CHECK_IN_FLIGHT.value,
                    Transaction.updated_at <= now - timedelta(seconds=30),
                )
            ).all()
        n = 0
        for txn_id, version in expired:
            try:
                self.ledger.escalate(
                    txn_id,
                    "simulated T+1 deadline reached without authoritative evidence",
                    expected_version=version,
                )
                n += 1
            except SafetyViolation:
                logging.getLogger(__name__).debug(
                    "best-effort step failed", exc_info=True
                )  # raced with a resolution; the newer state wins
        for txn_id, version in stuck:  # crash recovery: a check that never reported back
            try:
                self.ledger.apply_evidence(
                    txn_id, None, actor="DISPATCHER", expected_version=version
                )
            except SafetyViolation:
                logging.getLogger(__name__).debug("best-effort step failed", exc_info=True)
        return n

    # ------------------------------------------------------------------ one tick
    def tick(self, *, inline: bool = False) -> dict:
        if self.v11 is not None:
            return self.tick_v11(inline=inline)
        self._maybe_switch_controller()
        now = datetime.now(UTC)
        started = time.perf_counter()
        escalated = self.escalate_expired(now)
        candidates = self._candidates(now)
        if not candidates:
            return {"candidates": 0, "dispatched": 0, "escalated": escalated}
        measured = self._measured()
        backlog = self.backlog_by_route()
        defenses = self.ctx.runtime.defenses()
        routes = []
        for rid in sorted({c.route_id for c in candidates}):
            dest = rid.split("-", 1)[1]
            twin = self.twin.get(f"route:{rid}")
            integrity = self.twin.get(f"integrity:{rid}")
            routes.append(
                {
                    "route_id": rid,
                    "backlog": backlog.get(rid, 0),
                    "measured": measured.get(dest, {}),
                    "twin": twin[1] if twin else None,
                    "integrity": integrity[1] if integrity else None,
                }
            )
        req = RecoveryDecisionRequest(
            decision_request_id=f"drq-{uuid.uuid4().hex[:12]}",
            timestamp=now,
            routes=routes,
            candidates=[
                {
                    "transaction_id": c.transaction_id,
                    "route_id": c.route_id,
                    "version": c.version,
                    "checks_used": c.checks_used,
                    "amount_paise": c.amount_paise,
                    "ambiguous_since": c.ambiguous_since.isoformat() if c.ambiguous_since else None,
                    "deadline_at": c.deadline_at.isoformat(),
                }
                for c in candidates
            ],
        )
        decision = self.controller.decide(req)
        by_id = {c.transaction_id: c for c in candidates}
        applied: dict[str, dict] = {}
        selected: list[Transaction] = []
        for route in routes:
            rid = route["route_id"]
            recommended = decision.route_budgets.get(rid, self.guard.fallback)
            rb = self.guard.apply(
                rid,
                recommended,
                telemetry=route["integrity"],
                measured=route["measured"],
                enabled=defenses.telemetry_validation,
            )
            applied[rid] = rb.as_dict()
            ordered = [
                by_id[t]
                for t in decision.transaction_ids
                if t in by_id and by_id[t].route_id == rid
            ]
            ordered += [c for c in candidates if c.route_id == rid and c not in ordered]
            selected.extend(ordered[: rb.applied])
        dispatched = 0
        per_route: dict[str, int] = {}
        inflight = self.inflight_by_route()
        gate_refusals: dict[str, int] = {}
        route_measured = {r["route_id"]: r["measured"] for r in routes}
        for txn in selected:
            g = self.gate.check(
                txn,
                dispatched_on_route=per_route.get(txn.route_id, 0),
                route_budget=applied[txn.route_id]["applied"],
                inflight_on_route=inflight.get(txn.route_id, 0),
                measured=route_measured.get(txn.route_id),
            )
            if not g.allowed:
                gate_refusals[g.reason] = gate_refusals.get(g.reason, 0) + 1
                continue
            try:
                res = self.ledger.start_check(txn.transaction_id, txn.version)
            except SafetyViolation:
                continue
            self.queue.send(
                DispatchJob(
                    transaction_id=txn.transaction_id,
                    route_id=txn.route_id,
                    bank_id=txn.remitter_bank_id,
                    expected_version=res.new_version,
                    decision_id=decision.decision_id,
                    correlation_id=txn.correlation_id,
                )
            )
            dispatched += 1
            per_route[txn.route_id] = per_route.get(txn.route_id, 0) + 1
        if inline:
            self.queue.drain(self.handle_job)
        record = {
            "decision_id": decision.decision_id,
            "policy": decision.policy,
            "fallback": decision.fallback,
            "fallback_reason": decision.fallback_reason,
            "candidates": len(candidates),
            "dispatched": dispatched,
            "escalated": escalated,
            "budgets": applied,
            "gate_refusals": gate_refusals,
            "predicted": decision.predicted,
            "decision_ms": round((time.perf_counter() - started) * 1000, 2),
            "timestamp": now.isoformat(),
        }
        self.last_decision = record
        clamped = any(b["applied"] < b["recommended"] for b in applied.values())
        self.ctx.audit_buffer.record(
            make_record(
                "RECOVERY_DECISION",
                Decision.FALLBACK
                if decision.fallback
                else (Decision.REJECTED if clamped else Decision.ACCEPTED),
                "DISPATCHER",
                reason=decision.fallback_reason
                or ("budget clamped by guard" if clamped else "recommendation applied"),
                metadata=record,
            )
        )
        self.ctx.metrics.inc("dispatch_ticks")
        self.ctx.metrics.inc("checks_dispatched", dispatched)
        return record

    # ------------------------------------------------------------------ v1.1 tick
    def _rows(self, txn_ids) -> list[Transaction]:
        """Authoritative rows, read immediately before dispatch (not the decision-time view)."""
        if not txn_ids:
            return []
        with self.ctx.db.session() as s:
            return list(
                s.execute(
                    select(Transaction).where(Transaction.transaction_id.in_(list(txn_ids)))
                ).scalars()
            )

    def tick_v11(self, *, inline: bool = False) -> dict:
        """One v1.1 tick per route with recovery work.

        * DECISION: only the controller's selected transactions, only those the shared
          execution gate admits against rows re-read now, then Track B's pre-dispatch gate,
          then ``start_check``'s row-locked version compare-and-set. Nothing is padded in.
        * EXPLICIT_FAIL_CLOSED / WARMING_UP: zero checks and no fallback.
        * controller unavailable, timeout, malformed, expired, identity or policy mismatch:
          Track B's own labelled fallback (``fallback_budget_per_route``, oldest first),
          recorded with its true cause and never attributed to the controller.
        """
        from contracts.v1_1.protocol import ControllerOutcome

        now = datetime.now(UTC)
        escalated = self.escalate_expired(now)
        candidates = self._candidates(now)
        routes = sorted({c.route_id for c in candidates})
        inflight = self.inflight_by_route()
        measured = self._measured() if routes else {}
        per_route: dict[str, dict] = {}
        dispatched = 0
        for rid in routes:
            watermark = self.v11_watermark(rid) if self.v11_watermark else ()
            request, response, failure = self.v11.request(rid, now=now, watermark=watermark)
            route_cands = [c for c in candidates if c.route_id == rid]
            claimed: dict = {}  # what the controller said (audit only; never trusted for action)
            if failure is None:
                selected = []
                if response.status_code == 200:
                    body = response.json()
                    selected = list(body.get("selected_transaction_ids") or [])
                    claimed = {
                        k: body.get(k) for k in ("decision_mode", "reason_code", "budget_rps")
                    }
                rows = self._rows(selected)
                plan = self.v11.gate(
                    request,
                    response,
                    rows,
                    now=datetime.now(UTC),
                    rate_allowance=self.guard.hard_cap,
                )
                outcome, refused, decision_id = plan.outcome, dict(plan.refused), plan.decision_id
                fallback = plan.backend_fallback_permitted
                admitted_ids = list(plan.admitted)
            else:
                outcome, refused, decision_id, fallback, admitted_ids = failure, {}, None, True, []
            # ``decision_id`` names the controller decision that authorised the dispatched checks,
            # so it is set only for DECISION. Whatever the controller returned is kept as
            # ``controller_decision_id``; it never labels Track B's own fallback checks.
            controller_decision_id = decision_id
            if outcome is not ControllerOutcome.DECISION:
                decision_id = None
            if outcome is ControllerOutcome.DECISION:
                to_dispatch, source = self._rows(admitted_ids), "controller"
                order = {t: i for i, t in enumerate(admitted_ids)}
                to_dispatch.sort(key=lambda r: order[r.transaction_id])
                budget = len(to_dispatch)
            elif fallback:
                budget, source = self.guard.fallback, "trackb_fallback"
                to_dispatch = route_cands[:budget]
            else:  # explicit FAIL_CLOSED or WARMING_UP: zero discretionary checks
                budget, source, to_dispatch = 0, "fail_closed_honoured", []
            sent, gate_refusals = 0, {}
            for txn in to_dispatch:
                g = self.gate.check(
                    txn,
                    dispatched_on_route=sent,
                    route_budget=budget,
                    inflight_on_route=inflight.get(rid, 0),
                    measured=measured.get(rid.split("-")[1]),
                )
                if not g.allowed:
                    gate_refusals[g.reason] = gate_refusals.get(g.reason, 0) + 1
                    continue
                try:
                    res = self.ledger.start_check(txn.transaction_id, txn.version)
                except SafetyViolation as exc:  # version moved: another dispatcher won the race
                    gate_refusals[f"cas:{exc.code.value}"] = (
                        gate_refusals.get(f"cas:{exc.code.value}", 0) + 1
                    )
                    continue
                self.queue.send(
                    DispatchJob(
                        transaction_id=txn.transaction_id,
                        route_id=txn.route_id,
                        bank_id=txn.remitter_bank_id,
                        expected_version=res.new_version,
                        decision_id=decision_id or f"trackb-fallback-{uuid.uuid4().hex[:12]}",
                        correlation_id=txn.correlation_id,
                    )
                )
                sent += 1
            dispatched += sent
            per_route[rid] = {
                "outcome": outcome.value,
                "source": source,
                "decision_id": decision_id,
                "controller_decision_id": controller_decision_id,
                "selected": len(admitted_ids) + len(refused),
                "admitted": len(admitted_ids),
                "gate_refused": {k: v.value for k, v in refused.items()},
                "dispatched": sent,
                "predispatch_refused": gate_refusals,
                "controller_reason": claimed.get("reason_code"),
                "controller_mode": claimed.get("decision_mode"),
                "controller_budget_rps": claimed.get("budget_rps"),
            }
            self.ctx.audit_buffer.record(
                make_record(
                    "RECOVERY_DECISION_V11",
                    Decision.FALLBACK
                    if source == "trackb_fallback"
                    else (Decision.ACCEPTED if sent else Decision.DEFERRED),
                    "DISPATCHER",
                    route_id=rid,
                    experiment_id=self.ctx.settings.experiment_id,
                    reason=f"{outcome.value} -> {source}",
                    metadata=per_route[rid],
                )
            )
        if inline:
            self.queue.drain(self.handle_job)
        record = {
            "protocol": "v1.1",
            "routes": per_route,
            "dispatched": dispatched,
            "escalated": escalated,
            "candidates": len(candidates),
            "timestamp": now.isoformat(),
        }
        self.last_decision = record
        self.ctx.metrics.inc("dispatch_ticks")
        self.ctx.metrics.inc("checks_dispatched", dispatched)
        return record

    def reconcile_escalated(self, limit: int = 20) -> dict:
        """Periodic pass: try to resolve escalated transactions through the reconciliation channel."""
        with self.ctx.db.session() as s:
            ids = list(
                s.execute(
                    select(Transaction.transaction_id)
                    .where(Transaction.state == TxnState.HANDOFF_UDIR.value)
                    .order_by(Transaction.updated_at)
                    .limit(limit)
                ).scalars()
            )
        resolved = 0
        for txn_id in ids:
            try:
                resolved += int(
                    self.remitter.reconcile(txn_id, actor="RECON:DISPATCHER")["resolved"]
                )
            except SafetyViolation:
                continue  # raced with another resolver; the newer state wins
        self.ctx.metrics.inc("reconciliation_passes")
        return {"escalated_seen": len(ids), "resolved": resolved}

    # ------------------------------------------------------------------ job execution
    def handle_job(self, job: DispatchJob) -> None:
        try:
            outcome = self.remitter.check_status(
                job.transaction_id,
                expected_version=job.expected_version,
                as_bank=job.bank_id,
                correlation_id=job.correlation_id,
            )
        except SafetyViolation as exc:
            # Duplicate/stale delivery (e.g. SQS redelivery): version moved on, nothing to do.
            self.ctx.metrics.inc("dispatch_stale_jobs", reason=exc.code.value)
            return
        self.ctx.metrics.inc(
            "checks_admitted" if outcome["admitted"] else "checks_not_admitted", route=job.route_id
        )
        self.controller.observe(
            {
                "transaction_id": job.transaction_id,
                "route_id": job.route_id,
                "admitted": outcome["admitted"],
                "status": outcome.get("status"),
                "latency_ms": outcome.get("latency_ms"),
                "rejection_reason": outcome.get("rejection_reason"),
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )

    # ------------------------------------------------------------------ loop
    def run_forever(self) -> None:
        self.queue.start(self.handle_job)
        tick = self.ctx.settings.dispatch_tick_seconds
        log_event(logger, "dispatcher started", controller=self.controller.name, tick_s=tick)
        recon_every = self.ctx.settings.reconciliation_interval_seconds
        last_recon = time.monotonic()
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self.tick()
                if recon_every > 0 and t0 - last_recon >= recon_every:
                    last_recon = t0
                    self.reconcile_escalated()
                self._publish_stats()
            except Exception:  # noqa: BLE001 - keep the loop alive, log and retry next tick
                logger.exception("dispatcher tick failed")
            self._stop.wait(max(0.0, tick - (time.monotonic() - t0)))
        self.queue.stop()

    def stop(self) -> None:
        self._stop.set()


def main() -> None:
    from backend.aorts.app.config import get_settings
    from backend.aorts.app.dependencies import build_context
    from backend.aorts.app.logging_config import configure_logging
    from backend.aorts.banking.remitter.main import make_ledger

    settings = get_settings()
    configure_logging("dispatcher", settings.log_level)
    ctx = build_context(settings)
    ctx.audit_buffer.start()
    dispatcher = Dispatcher(ctx, RemitterService(ctx, make_ledger(settings, ctx)))
    signal.signal(signal.SIGTERM, lambda *_: dispatcher.stop())
    try:
        dispatcher.run_forever()
    except KeyboardInterrupt:
        dispatcher.stop()
    finally:
        ctx.audit_buffer.stop()


if __name__ == "__main__":
    main()
