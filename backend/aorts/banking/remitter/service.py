"""Remitter bank orchestration: debit -> forward to switch -> interpret the answer.

Interpretation rule (rule C): only a signature-verified HTTP 200 from the switch
whose body names the same transaction and says SUCCESS or FAILED is evidence.
Timeouts, connection errors, 5xx, unsigned or mismatched answers leave the
transaction AMBIGUOUS.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

import httpx

from backend.aorts.app.dependencies import ServiceContext
from backend.aorts.app.logging_config import log_event
from backend.aorts.banking.ledger.invariants import SafetyViolation
from backend.aorts.banking.ledger.service import LedgerService
from backend.aorts.contracts.enums import ErrorCode, SwitchStatus, TxnState
from backend.aorts.contracts.requests import (
    PaymentRequest,
    StatusCheckRequest,
    StatusEvidence,
    SwitchPayRequest,
)
from backend.aorts.contracts.responses import OperationResult
from backend.aorts.security.request_auth import verify_response

logger = logging.getLogger("aorts.remitter")

SIMULATIONS = {"hold", "lost-response", "timeout"}


class RemitterService:
    def __init__(self, ctx: ServiceContext, ledger: LedgerService):
        self.ctx = ctx
        self.ledger = ledger

    # ------------------------------------------------------------------ evidence
    def evidence_from_response(
        self, txn_id: str, resp: httpx.Response, path: str
    ) -> StatusEvidence | None:
        if resp.status_code != 200:
            return None
        authentic = verify_response(
            self.ctx.registry,
            "SWITCH",
            resp.status_code,
            path,
            resp.headers,
            resp.content,
            self.ctx.settings.max_clock_skew_seconds,
        )
        try:
            body = resp.json()
        except ValueError:
            return None
        if not authentic:
            self.ctx.metrics.inc("unauthenticated_switch_responses")
            log_event(
                logger, "switch response failed signature verification", logging.WARNING, txn=txn_id
            )
        return StatusEvidence(
            transaction_id=str(body.get("transaction_id")),
            status=SwitchStatus(body.get("status", "PENDING")),
            source="SWITCH" if authentic else "UNVERIFIED",
            switch_reference=body.get("switch_reference"),
            observed_at=datetime.now(UTC),
            authenticated=authentic,
            reason=body.get("reason"),
        )

    # ------------------------------------------------------------------ payments
    def create_payment(self, req: PaymentRequest, simulate: str | None = None):
        m = self.ctx.metrics
        txn, replayed = self.ledger.create_payment(req, actor="REMITTER")
        m.inc("payments_replayed" if replayed else "payments_created", route=txn.route_id)
        if replayed or simulate == "hold":
            return self._reload(txn.transaction_id), replayed
        if simulate == "timeout":
            self.ledger.mark_ambiguous(
                txn.transaction_id, "simulated timeout: request never reached the switch"
            )
        else:
            self.forward(txn.transaction_id, drop_response=(simulate == "lost-response"))
        return self._reload(txn.transaction_id), replayed

    def forward(self, txn_id: str, *, drop_response: bool = False) -> OperationResult:
        txn = self._reload(txn_id)
        pay = SwitchPayRequest(
            transaction_id=txn.transaction_id,
            remitter_bank_id=txn.remitter_bank_id,
            beneficiary_bank_id=txn.beneficiary_bank_id,
            beneficiary_account_id=txn.beneficiary_account_id,
            route_id=txn.route_id,
            amount_paise=txn.amount_paise,
            correlation_id=txn.correlation_id,
        )
        m = self.ctx.metrics
        started = time.perf_counter()
        reason = None
        evidence = None
        local_fault = self.ctx.faults.decide("remitter")
        try:
            resp = self.ctx.client.post(
                "switch",
                "/api/v1/pay",
                pay.model_dump(mode="json"),
                as_identity=txn.remitter_bank_id,
                timeout=self.ctx.settings.remitter_switch_timeout_s,
            )
            if drop_response or local_fault.drop_response:
                reason = "response lost (simulated)"
            else:
                evidence = self.evidence_from_response(txn_id, resp, "/api/v1/pay")
                reason = f"switch HTTP {resp.status_code}" if evidence is None else None
        except httpx.TimeoutException:
            reason = "switch timeout"
        except httpx.HTTPError as exc:
            reason = f"switch unreachable: {type(exc).__name__}"
        m.observe_latency("pay_latency", (time.perf_counter() - started) * 1000, route=txn.route_id)
        if evidence is not None and not evidence.authenticated:
            reason, evidence = "switch response failed signature verification", None
        if evidence is not None and evidence.transaction_id != txn_id:
            reason, evidence = "switch response names a different transaction", None
        if evidence is None or evidence.status in (SwitchStatus.PENDING, SwitchStatus.NOT_FOUND):
            m.inc("payments_ambiguous", route=txn.route_id)
            why = reason or f"switch answered {evidence.status if evidence else 'nothing'}"
            return self.ledger.mark_ambiguous(txn_id, why)
        result = self.ledger.apply_evidence(txn_id, evidence, actor="REMITTER")
        m.inc("payments_resolved", route=txn.route_id, outcome=result.financial_outcome or "?")
        return result

    def _reload(self, txn_id: str):
        from backend.aorts.banking.ledger.models import Transaction

        with self.ctx.db.session() as s:
            return s.get(Transaction, txn_id)

    # ------------------------------------------------------------------ status checks
    def check_status(
        self,
        txn_id: str,
        *,
        expected_version: int,
        as_bank: str | None = None,
        correlation_id: str | None = None,
    ) -> dict:
        """Perform one signed /check-status for a CHECK_IN_FLIGHT transaction and apply the result."""
        txn = self._reload(txn_id)
        bank = as_bank or txn.remitter_bank_id
        req = StatusCheckRequest(
            transaction_id=txn_id,
            bank_id=bank,
            route_id=txn.route_id,
            correlation_id=correlation_id or txn.correlation_id,
        )
        started = time.perf_counter()
        try:
            resp = self.ctx.client.post(
                "switch",
                "/api/v1/check-status",
                req.model_dump(mode="json"),
                as_identity=bank,
                timeout=self.ctx.settings.status_check_timeout_s,
            )
        except httpx.HTTPError as exc:
            latency = (time.perf_counter() - started) * 1000
            # The request may have been admitted; count the attempt and keep the uncertainty.
            res = self.ledger.apply_evidence(
                txn_id,
                None,
                actor="DISPATCHER",
                expected_version=expected_version,
                count_check=True,
            )
            return {
                "admitted": True,
                "status": None,
                "latency_ms": latency,
                "error": type(exc).__name__,
                "result": res.model_dump(),
            }
        latency = (time.perf_counter() - started) * 1000
        if resp.status_code in (429, 403):
            body = resp.json()
            res = self.ledger.check_not_admitted(
                txn_id, expected_version, body.get("error_code", "REJECTED")
            )
            return {
                "admitted": False,
                "status": None,
                "latency_ms": latency,
                "rejection_reason": body.get("error_code"),
                "result": res.model_dump(),
            }
        evidence = self.evidence_from_response(txn_id, resp, "/api/v1/check-status")
        if evidence is not None and evidence.transaction_id != txn_id:
            evidence = None  # never apply an answer about a different transaction
        res = self.ledger.apply_evidence(
            txn_id,
            evidence if evidence and evidence.authenticated else None,
            actor="DISPATCHER",
            expected_version=expected_version,
            count_check=True,
        )
        return {
            "admitted": True,
            "status": evidence.status.value if evidence else None,
            "latency_ms": latency,
            "result": res.model_dump(),
        }

    # ------------------------------------------------------------------ reconciliation (HANDOFF_UDIR)
    def reconcile(self, txn_id: str, *, actor: str = "RECONCILIATION") -> dict:
        """Resolve an escalated transaction through the switch's low-rate reconciliation channel.

        Same evidence rules as everywhere else: only a signed, correctly-linked SUCCESS/FAILED moves it to
        RESOLVED_BY_RECONCILIATION (with settlement or exactly-once reversal); otherwise it stays escalated.
        """
        txn = self._reload(txn_id)
        if txn is None:
            raise SafetyViolation(ErrorCode.TRANSACTION_NOT_FOUND, "transaction not found", 404)
        if txn.state != TxnState.HANDOFF_UDIR:
            raise SafetyViolation(
                ErrorCode.ILLEGAL_TRANSITION, f"reconciliation needs HANDOFF_UDIR, is {txn.state}"
            )
        req = StatusCheckRequest(
            transaction_id=txn_id,
            bank_id=txn.remitter_bank_id,
            route_id=txn.route_id,
            correlation_id=txn.correlation_id,
        )
        path = "/api/v1/reconciliation/status"
        try:
            resp = self.ctx.client.post(
                "switch",
                path,
                req.model_dump(mode="json"),
                as_identity=txn.remitter_bank_id,
                timeout=self.ctx.settings.status_check_timeout_s,
            )
        except httpx.HTTPError as exc:
            return {
                "transaction_id": txn_id,
                "resolved": False,
                "reason": f"switch unreachable: {type(exc).__name__}",
            }
        if resp.status_code != 200:
            code = resp.json().get("error_code") if resp.content else str(resp.status_code)
            return {"transaction_id": txn_id, "resolved": False, "reason": code}
        evidence = self.evidence_from_response(txn_id, resp, path)
        if (
            evidence is None
            or not evidence.authenticated
            or evidence.transaction_id != txn_id
            or evidence.status not in (SwitchStatus.SUCCESS, SwitchStatus.FAILED)
        ):
            return {
                "transaction_id": txn_id,
                "resolved": False,
                "reason": f"no authoritative outcome yet ({evidence.status if evidence else 'unverified'})",
            }
        result = self.ledger.apply_evidence(
            txn_id, evidence, actor=actor, expected_version=txn.version
        )
        self.ctx.metrics.inc("reconciled", outcome=result.financial_outcome or "?")
        return {"transaction_id": txn_id, "resolved": True, "result": result.model_dump()}
