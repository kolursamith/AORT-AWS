"""Mock payment switch logic.

The switch keeps its own authoritative record (``switch_transactions``) so it can
answer status checks consistently. Key safety property: the switch answers FAILED
only when it *knows* the beneficiary did not and cannot later credit, either
because it never forwarded the payment, the beneficiary declined it, or the
beneficiary has been *fenced* (see ``beneficiary/service.py``). Otherwise it
answers PENDING and the remitter keeps the transaction AMBIGUOUS.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

import httpx
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from backend.aorts.app.dependencies import ServiceContext
from backend.aorts.app.logging_config import log_event
from backend.aorts.banking.ledger.models import SwitchTransaction
from backend.aorts.banking.switch.capacity import CapacityModel, DestinationPool
from backend.aorts.contracts.enums import Decision, ErrorCode, SwitchStatus
from backend.aorts.contracts.events import transaction_id_bank
from backend.aorts.contracts.requests import (
    BeneficiaryCreditRequest,
    StatusCheckRequest,
    SwitchPayRequest,
)
from backend.aorts.contracts.responses import StatusCheckResponse, SwitchPayResponse
from backend.aorts.security.audit import make_record
from backend.aorts.security.defense_policy import AdmissionController, AdmissionDecision
from backend.aorts.security.rate_limiter import TokenBucket
from backend.aorts.security.request_auth import VerifiedIdentity

logger = logging.getLogger("aorts.switch")


class ResponseLost(Exception):
    """Fault injection: the switch finished processing but the response never arrives."""


class SwitchError(Exception):
    def __init__(self, code: ErrorCode, message: str, status: int, retry_after: float = 0.0):
        super().__init__(message)
        self.code, self.message, self.status, self.retry_after = code, message, status, retry_after


class SwitchService:
    def __init__(self, ctx: ServiceContext):
        self.ctx = ctx
        s = ctx.settings
        self.destinations = sorted(s.beneficiary_url_map())
        self.capacity = CapacityModel(
            self.destinations, s.switch_workers_per_destination, s.switch_queue_wait_s
        )
        # Switch-wide processing capacity shared by ALL admitted requests (payments and status checks).
        # Flooding exhausts this pool unless the admission defenses reject the excess first.
        self.core = DestinationPool("core", s.switch_core_workers, s.switch_queue_wait_s)
        # Bulkhead (defense in depth): with switch_check_workers > 0, status checks get their OWN pool,
        # so even a flood that slips past admission control cannot starve payment processing.
        # 0 = shared pool (the configuration used for the first flooding experiments).
        self.check_pool = (
            DestinationPool("checks", s.switch_check_workers, s.switch_queue_wait_s)
            if s.switch_check_workers > 0
            else self.core
        )
        self.base_processing_s = s.switch_base_processing_ms / 1000.0
        self.primary_reserve = s.switch_primary_reserve_fraction
        self.admission = AdmissionController(
            bank_rate=s.bank_check_rate_per_sec,
            bank_burst=s.bank_check_burst,
            route_rate=s.route_check_rate_per_sec,
            route_burst=s.route_check_burst,
            global_rate=s.global_check_rate_per_sec,
            global_burst=s.global_check_burst,
            credit_cap=s.tier2_credit_cap,
            credit_refill=s.tier2_credit_refill_per_sec,
        )
        self.started_at = time.time()
        # Reconciliation is a separate, deliberately slow channel (UDIR-style dispute handling):
        # its own per-bank bucket, no Tier 2 credits, so it cannot be used to bypass flood limits.
        self.recon_buckets: dict[str, TokenBucket] = {}
        self.recon_rate, self.recon_burst = s.reconciliation_rate_per_sec, s.reconciliation_burst

    # ------------------------------------------------------------------ helpers
    def _refresh_capacity(self) -> None:
        self.capacity.apply_overrides(
            {d: self.ctx.faults.capacity_override(f"route:{d}") for d in self.destinations}
        )

    def _record(self, txn_id: str) -> SwitchTransaction | None:
        with self.ctx.db.session() as s:
            return s.get(SwitchTransaction, txn_id)

    def _set_status(
        self,
        txn_id: str,
        status: SwitchStatus,
        reason: str | None,
        forward_state: str | None = None,
        only_if_pending: bool = True,
    ) -> None:
        values = {
            "status": status.value,
            "reason": reason,
            "updated_at": datetime.now(UTC),
            "version": SwitchTransaction.version + 1,
        }
        if forward_state:
            values["forward_state"] = forward_state
        stmt = update(SwitchTransaction).where(SwitchTransaction.transaction_id == txn_id)
        if only_if_pending:
            stmt = stmt.where(SwitchTransaction.status == SwitchStatus.PENDING.value)
        with self.ctx.db.session() as s:
            s.execute(stmt.values(**values))

    def _set_forward_state(self, txn_id: str, forward_state: str) -> None:
        with self.ctx.db.session() as s:
            s.execute(
                update(SwitchTransaction)
                .where(SwitchTransaction.transaction_id == txn_id)
                .values(forward_state=forward_state, updated_at=datetime.now(UTC))
            )

    # ------------------------------------------------------------------ /pay
    def pay(self, req: SwitchPayRequest, ident: VerifiedIdentity) -> SwitchPayResponse:
        m = self.ctx.metrics
        m.inc("pay_received", route=req.route_id)
        if (
            ident.identity != req.remitter_bank_id
            or req.route_id != f"{req.remitter_bank_id}-{req.beneficiary_bank_id}"
        ):
            m.inc("pay_rejected", reason="IDENTITY_MISMATCH")
            raise SwitchError(
                ErrorCode.IDENTITY_MISMATCH, "remitter identity / route mismatch", 403
            )
        self._require_namespace(req.transaction_id, ident, "pay_rejected")
        if req.beneficiary_bank_id not in self.destinations:
            m.inc("pay_rejected", reason="INVALID_ROUTE")
            raise SwitchError(
                ErrorCode.INVALID_ROUTE, f"unknown beneficiary bank {req.beneficiary_bank_id}", 422
            )
        fault = self.ctx.faults.decide("switch", stream="pay", key=req.transaction_id)
        if fault.unavailable:
            m.inc("pay_rejected", reason="UNAVAILABLE")
            raise SwitchError(
                ErrorCode.DEPENDENCY_UNAVAILABLE, "switch unavailable (injected fault)", 503
            )

        ref = f"SW-{req.transaction_id}"
        with self.ctx.db.session() as s:
            inserted = s.execute(
                insert(SwitchTransaction)
                .values(
                    transaction_id=req.transaction_id,
                    remitter_bank_id=req.remitter_bank_id,
                    beneficiary_bank_id=req.beneficiary_bank_id,
                    beneficiary_account_id=req.beneficiary_account_id,
                    route_id=req.route_id,
                    amount_paise=req.amount_paise,
                    status=SwitchStatus.PENDING.value,
                    forward_state="NOT_FORWARDED",
                    switch_reference=ref,
                )
                .on_conflict_do_nothing(index_elements=["transaction_id"])
                .returning(SwitchTransaction.transaction_id)
            ).scalar()
        if inserted is None:
            # Duplicate /pay (or a status-check fence got there first): never forward twice.
            existing = self._record(req.transaction_id)
            if existing.remitter_bank_id != ident.identity:  # never reveal another bank's record
                m.inc("pay_rejected", reason="WRONG_BANK")
                raise SwitchError(
                    ErrorCode.IDENTITY_MISMATCH, "transaction belongs to another remitter bank", 403
                )
            m.inc("pay_duplicate")
            return SwitchPayResponse(
                transaction_id=req.transaction_id,
                status=SwitchStatus(existing.status),
                switch_reference=existing.switch_reference,
                reason=existing.reason or "duplicate",
            )

        self.ctx.faults.sleep(fault)
        with self.core.slot() as core_ok:
            if not core_ok:
                self._set_status(
                    req.transaction_id, SwitchStatus.FAILED, "SWITCH_OVERLOADED", "NOT_FORWARDED"
                )
                m.inc("pay_completed", route=req.route_id, status="FAILED_OVERLOAD")
                m.inc("core_overloaded", kind="pay")
                return SwitchPayResponse(
                    transaction_id=req.transaction_id,
                    status=SwitchStatus.FAILED,
                    switch_reference=ref,
                    reason="SWITCH_OVERLOADED",
                )
            time.sleep(self.base_processing_s)
        self._refresh_capacity()
        pool = self.capacity.pool(req.beneficiary_bank_id)
        started = time.perf_counter()
        with pool.slot() as ok:
            if not ok:
                self._set_status(
                    req.transaction_id, SwitchStatus.FAILED, "SWITCH_OVERLOADED", "NOT_FORWARDED"
                )
                m.inc("pay_completed", route=req.route_id, status="FAILED_OVERLOAD")
                return SwitchPayResponse(
                    transaction_id=req.transaction_id,
                    status=SwitchStatus.FAILED,
                    switch_reference=ref,
                    reason="SWITCH_OVERLOADED",
                )
            self._set_forward_state(req.transaction_id, "FORWARDING")
            status, reason = self._forward_credit(req)
        latency_ms = (time.perf_counter() - started) * 1000
        m.observe_latency("route_latency", latency_ms, destination=req.beneficiary_bank_id)
        if status == SwitchStatus.PENDING:
            m.inc("pay_timeout", destination=req.beneficiary_bank_id)
            self._set_forward_state(req.transaction_id, "DONE")
        else:
            self._set_status(req.transaction_id, status, reason, "DONE")
        m.inc("pay_completed", route=req.route_id, status=status.value)
        m.inc("dest_completed", destination=req.beneficiary_bank_id)
        route_fault = self.ctx.faults.decide(f"route:{req.beneficiary_bank_id}", stream="pay-route")
        if fault.drop_response or route_fault.drop_response:
            m.inc("pay_response_dropped", route=req.route_id)
            raise ResponseLost()
        return SwitchPayResponse(
            transaction_id=req.transaction_id, status=status, switch_reference=ref, reason=reason
        )

    def _forward_credit(self, req: SwitchPayRequest) -> tuple[SwitchStatus, str | None]:
        credit = BeneficiaryCreditRequest(
            transaction_id=req.transaction_id,
            operation_id=f"{req.transaction_id}:CREDIT",
            beneficiary_account_id=req.beneficiary_account_id,
            amount_paise=req.amount_paise,
            route_id=req.route_id,
        )
        target = f"beneficiary:{req.beneficiary_bank_id}"
        self.ctx.metrics.inc("downstream_calls", destination=req.beneficiary_bank_id, kind="credit")
        try:
            resp = self.ctx.client.post(
                target,
                "/api/v1/beneficiary/credit",
                credit.model_dump(mode="json"),
                as_identity="SWITCH",
                timeout=self.ctx.settings.switch_beneficiary_timeout_s,
            )
        except httpx.HTTPError as exc:
            log_event(
                logger,
                "beneficiary call failed",
                logging.WARNING,
                txn=req.transaction_id,
                error=type(exc).__name__,
            )
            return SwitchStatus.PENDING, f"beneficiary {type(exc).__name__}"
        if resp.status_code == 200:
            body = resp.json()
            return SwitchStatus(body["status"]), body.get("reason")
        return SwitchStatus.PENDING, f"beneficiary HTTP {resp.status_code}"

    # ------------------------------------------------------------------ /check-status
    def check_status(self, req: StatusCheckRequest, ident: VerifiedIdentity) -> StatusCheckResponse:
        m = self.ctx.metrics
        m.inc("check_received", bank=ident.identity)
        defenses = self.ctx.runtime.defenses()
        decision: AdmissionDecision = self.admission.admit(
            ident.identity,
            req.bank_id,
            req.route_id,
            rate_limiting=defenses.rate_limiting,
            credits=defenses.tier2_credits,
        )
        m.observe_latency("admission_decision", decision.decision_ms)
        if not decision.admitted:
            m.inc("check_rejected", bank=ident.identity, reason=decision.code.value)
            m.inc("check_rejected_total")  # unlabelled: stable CloudWatch alarm target
            self.ctx.audit_buffer.record(
                make_record(
                    "STATUS_CHECK_REJECTED",
                    Decision.REJECTED,
                    ident.identity,
                    reason=decision.code.value,
                    transaction_id=req.transaction_id,
                    route_id=req.route_id,
                    correlation_id=req.correlation_id,
                    metadata={"detail": decision.reason},
                ),
                sample_key=(ident.identity, decision.code.value),
            )
            status = 403 if decision.code == ErrorCode.IDENTITY_MISMATCH else 429
            raise SwitchError(decision.code, decision.reason, status, decision.retry_after)
        m.inc("check_admitted", bank=ident.identity)
        self.ctx.audit_buffer.record(
            make_record(
                "STATUS_CHECK_ADMITTED",
                Decision.ACCEPTED,
                ident.identity,
                transaction_id=req.transaction_id,
                route_id=req.route_id,
                correlation_id=req.correlation_id,
            ),
            sample_key=(ident.identity, "ADMITTED"),
        )
        started = time.perf_counter()
        # Shared pool: status checks may not occupy the slots reserved for primary payments.
        limit = (
            self.core.limit_for_recovery(self.primary_reserve)
            if self.check_pool is self.core
            else None
        )
        with self.check_pool.slot(limit=limit) as core_ok:
            if not core_ok:
                m.inc("core_overloaded", kind="check", bank=ident.identity)
                raise SwitchError(
                    ErrorCode.SWITCH_OVERLOADED, "switch processing capacity exhausted", 503, 1.0
                )
            time.sleep(self.base_processing_s)
            result = self._lookup(req, ident)
        m.observe_latency(
            "check_service_latency", (time.perf_counter() - started) * 1000, bank=ident.identity
        )
        m.inc("check_completed", bank=ident.identity)
        return result

    def reconciliation_status(
        self, req: StatusCheckRequest, ident: VerifiedIdentity
    ) -> StatusCheckResponse:
        m = self.ctx.metrics
        if ident.identity != req.bank_id or not req.route_id.startswith(f"{ident.identity}-"):
            raise SwitchError(
                ErrorCode.IDENTITY_MISMATCH, "reconciliation identity / route mismatch", 403
            )
        with self.admission._lock:
            bucket = self.recon_buckets.setdefault(
                ident.identity, TokenBucket(self.recon_rate, self.recon_burst)
            )
            if not bucket.available():
                m.inc("recon_rejected", bank=ident.identity)
                raise SwitchError(
                    ErrorCode.RATE_LIMITED, "reconciliation rate limit", 429, bucket.retry_after()
                )
            bucket.take()
        m.inc("recon_admitted", bank=ident.identity)
        with self.check_pool.slot() as ok:
            if not ok:
                raise SwitchError(
                    ErrorCode.SWITCH_OVERLOADED, "switch processing capacity exhausted", 503, 1.0
                )
            return self._lookup(req, ident)

    def _require_namespace(self, txn_id: str, ident: VerifiedIdentity, counter: str) -> None:
        owner = transaction_id_bank(txn_id)
        if owner is not None and owner != ident.identity:
            self.ctx.metrics.inc(counter, bank=ident.identity, reason="FOREIGN_TRANSACTION_ID")
            self.ctx.audit_buffer.record(
                make_record(
                    "FOREIGN_TRANSACTION_ID",
                    Decision.REJECTED,
                    ident.identity,
                    transaction_id=txn_id,
                    reason=f"transaction id belongs to {owner}",
                ),
                sample_key=(ident.identity, "FOREIGN_TRANSACTION_ID"),
            )
            raise SwitchError(
                ErrorCode.IDENTITY_MISMATCH, "transaction id belongs to another remitter bank", 403
            )

    def _lookup(self, req: StatusCheckRequest, ident: VerifiedIdentity) -> StatusCheckResponse:
        m = self.ctx.metrics
        # Bind the id to the signer BEFORE anything else: an unknown id would otherwise be fenced
        # (FAILED tombstone) on behalf of whichever bank asked first.
        self._require_namespace(req.transaction_id, ident, "check_rejected")
        rec = self._record(req.transaction_id)
        if rec is None:
            # Never received: insert a FAILED tombstone so a late /pay can no longer be processed.
            with self.ctx.db.session() as s:
                s.execute(
                    insert(SwitchTransaction)
                    .values(
                        transaction_id=req.transaction_id,
                        remitter_bank_id=ident.identity,
                        beneficiary_bank_id=req.route_id.split("-", 1)[1],
                        beneficiary_account_id="UNKNOWN",
                        route_id=req.route_id,
                        amount_paise=1,
                        status=SwitchStatus.FAILED.value,
                        forward_state="NOT_FORWARDED",
                        reason="NOT_RECEIVED_FENCED",
                        switch_reference=f"SW-{req.transaction_id}",
                    )
                    .on_conflict_do_nothing(index_elements=["transaction_id"])
                )
            rec = self._record(req.transaction_id)
        if rec.remitter_bank_id != ident.identity:
            m.inc("check_rejected", bank=ident.identity, reason="WRONG_BANK")
            raise SwitchError(
                ErrorCode.IDENTITY_MISMATCH, "transaction belongs to another remitter bank", 403
            )
        if rec.status != SwitchStatus.PENDING.value:
            return StatusCheckResponse(
                transaction_id=rec.transaction_id,
                status=SwitchStatus(rec.status),
                switch_reference=rec.switch_reference,
                route_id=rec.route_id,
                reason=rec.reason,
                served_from="switch_record",
            )
        # Unknown at the switch: ask the beneficiary (consumes downstream capacity).
        self._refresh_capacity()
        pool = self.capacity.pool(rec.beneficiary_bank_id)
        started = time.perf_counter()
        # Recovery traffic may never consume the destination slots reserved for primary payments.
        with pool.slot(limit=pool.limit_for_recovery(self.primary_reserve)) as ok:
            if not ok:
                m.inc("check_overloaded", destination=rec.beneficiary_bank_id)
                m.inc("recovery_blocked_by_primary_reserve", destination=rec.beneficiary_bank_id)
                raise SwitchError(
                    ErrorCode.SWITCH_OVERLOADED,
                    "destination capacity exhausted (remaining slots reserved for payments)",
                    503,
                    1.0,
                )
            m.inc("check_forwarded", destination=rec.beneficiary_bank_id)
            status, reason = self._query_beneficiary(rec)
        m.observe_latency(
            "check_latency",
            (time.perf_counter() - started) * 1000,
            destination=rec.beneficiary_bank_id,
        )
        if status in (SwitchStatus.SUCCESS, SwitchStatus.FAILED):
            self._set_status(rec.transaction_id, status, reason)
        return StatusCheckResponse(
            transaction_id=rec.transaction_id,
            status=status,
            switch_reference=rec.switch_reference,
            route_id=rec.route_id,
            reason=reason,
            served_from="beneficiary",
        )

    def _query_beneficiary(self, rec: SwitchTransaction) -> tuple[SwitchStatus, str | None]:
        target = f"beneficiary:{rec.beneficiary_bank_id}"
        self.ctx.metrics.inc("downstream_calls", destination=rec.beneficiary_bank_id, kind="status")
        timeout = self.ctx.settings.switch_beneficiary_timeout_s
        try:
            if rec.forward_state == "DONE":
                # Our forward attempt is over: fencing makes "not credited" a permanent fact.
                resp = self.ctx.client.post(
                    target,
                    f"/api/v1/beneficiary/transactions/{rec.transaction_id}/fence",
                    {},
                    as_identity="SWITCH",
                    timeout=timeout,
                )
            else:
                resp = self.ctx.client.get(
                    target,
                    f"/api/v1/beneficiary/transactions/{rec.transaction_id}",
                    as_identity="SWITCH",
                    timeout=timeout,
                )
        except httpx.HTTPError as exc:
            return SwitchStatus.PENDING, f"beneficiary {type(exc).__name__}"
        if resp.status_code != 200:
            return SwitchStatus.PENDING, f"beneficiary HTTP {resp.status_code}"
        body = resp.json()
        status = SwitchStatus(body["status"])
        if status == SwitchStatus.NOT_FOUND:
            return SwitchStatus.PENDING, "beneficiary has no record yet"
        return status, body.get("reason")

    # ------------------------------------------------------------------ observability
    def route_measurements(self, window_s: float = 30.0) -> dict[str, dict]:
        """Independent switch-side measurements used to validate twin telemetry (Module 2)."""
        m = self.ctx.metrics
        out = {}
        for dest in self.destinations:
            lat = m.latency_stats("route_latency", window_s, destination=dest)
            completed = m.rate("dest_completed", int(window_s), destination=dest)
            timeouts = m.rate("pay_timeout", int(window_s), destination=dest)
            pool = self.capacity.pool(dest).view()
            out[dest] = {
                "latency_p50_ms": lat["p50_ms"],
                "latency_p95_ms": lat["p95_ms"],
                "samples": lat["count"],
                "throughput_tps": round(completed, 3),
                "timeout_rate": round(timeouts / completed, 4) if completed else 0.0,
                "backlog": pool["waiting"],
                "in_flight": pool["in_flight"],
                "workers": pool["workers"],
            }
        return out

    def metrics_view(self) -> dict:
        snap = self.ctx.metrics.snapshot()
        return {
            "service": "switch",
            "uptime_s": round(time.time() - self.started_at, 1),
            "counters": snap["counters"],
            "capacity": self.capacity.view(),
            "core": self.core.view(),
            "check_pool": self.check_pool.view()
            if self.check_pool is not self.core
            else "shared-with-core",
            "routes": self.route_measurements(),
            "defenses": self.ctx.runtime.defenses().model_dump(),
            "audit_suppressed": self.ctx.audit_buffer.suppressed,
        }

    def all_records(self, limit: int = 100) -> list[dict]:
        with self.ctx.db.session() as s:
            rows = s.execute(
                select(SwitchTransaction).order_by(SwitchTransaction.created_at.desc()).limit(limit)
            ).scalars()
            return [
                {
                    "transaction_id": r.transaction_id,
                    "status": r.status,
                    "route_id": r.route_id,
                    "reason": r.reason,
                    "forward_state": r.forward_state,
                }
                for r in rows
            ]
