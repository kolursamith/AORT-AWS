"""Remitter bank orchestration: debit -> forward to switch -> interpret the answer.

Interpretation rule (rule C): only a signature-verified HTTP 200 from the switch
whose body names the same transaction and says SUCCESS or FAILED is evidence.
Timeouts, connection errors, 5xx, unsigned or mismatched answers leave the
transaction AMBIGUOUS.
"""

from __future__ import annotations

import logging
import re
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
from backend.aorts.security.request_auth import (
    ALGORITHM,
    H_RESPONSE_KEY_ID,
    H_RESPONSE_SIGNATURE,
    H_RESPONSE_TIMESTAMP,
    body_digest,
    verify_response,
)

logger = logging.getLogger("aorts.remitter")

SIMULATIONS = {"hold", "lost-response", "timeout"}
# Fields covered by the switch's response signature (request_auth._response_message).
SIGNED_RESPONSE_FIELDS = ["http_status", "path", "timestamp", "body_sha256"]

_DIGITS = re.compile(r"^[0-9]+$")
_HEX = re.compile(r"^[0-9a-f]+$")
_TOKEN = re.compile(r"^[A-Za-z0-9._:-]+$")


def _fit(value, limit: int, pattern: re.Pattern | None = None) -> str | None:
    """``value`` as stored evidence text, or None when it is absent or not well-formed."""

    if value is None or isinstance(value, (dict, list)):
        return None
    text_value = str(value)
    if not text_value or len(text_value) > limit:
        return None
    if pattern is not None and not pattern.match(text_value):
        return None
    return text_value


# Switch answers that are not a financial outcome; the switch's own words are kept.
INCONCLUSIVE = {SwitchStatus.PENDING: "PENDING", SwitchStatus.NOT_FOUND: "UNKNOWN"}


class RemitterService:
    def __init__(self, ctx: ServiceContext, ledger: LedgerService):
        self.ctx = ctx
        self.ledger = ledger

    # ------------------------------------------------------------------ evidence
    def evidence_from_response(
        self,
        txn_id: str,
        resp: httpx.Response,
        path: str,
        *,
        kind: str = "ORIGINAL_PAYMENT_RESPONSE",
        check_id: str | None = None,
        latency_ms: float | None = None,
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
        if not isinstance(body, dict):  # not an answer object: no evidence at all
            return None
        if not authentic:
            self.ctx.metrics.inc("unauthenticated_switch_responses")
            log_event(
                logger, "switch response failed signature verification", logging.WARNING, txn=txn_id
            )
        observed = datetime.now(UTC)
        evidence_id = self._record_evidence(
            txn_id, resp, path, body, authentic, observed, kind, check_id, latency_ms
        )
        return StatusEvidence(
            transaction_id=str(body.get("transaction_id")),
            status=SwitchStatus(body.get("status", "PENDING")),
            source="SWITCH" if authentic else "UNVERIFIED",
            switch_reference=body.get("switch_reference"),
            observed_at=observed,
            authenticated=authentic,
            reason=body.get("reason"),
            evidence_id=evidence_id,
        )

    def _record_evidence(
        self, txn_id, resp, path, body, authentic, observed, kind, check_id, latency_ms
    ) -> str:
        """Persist the received response and its verification result (v1.1 provenance).

        The signature material is stored exactly as received; ``verified`` is the result of
        ``verify_response`` above. Nothing is signed or asserted here. A header or field that is
        not well-formed (duplicated, oversized, wrong alphabet) is stored as NULL instead of
        failing the check: such a response can never verify (``verify_response`` rejects it).
        """
        from backend.aorts.banking.ledger.models import EvidenceRecord
        from backend.aorts.contracts.events import new_id

        evidence_id = new_id("evd")
        headers = resp.headers
        with self.ctx.db.session() as s:
            s.add(
                EvidenceRecord(
                    evidence_id=evidence_id,
                    transaction_id=txn_id,
                    evidence_kind=kind,
                    issuer_id="SWITCH",
                    path=path,
                    http_status=resp.status_code,
                    reported_status=_fit(body.get("status"), 16),
                    reported_transaction_id=_fit(body.get("transaction_id"), 80),
                    body_sha256=body_digest(resp.content),
                    response_timestamp=_fit(headers.get(H_RESPONSE_TIMESTAMP), 20, _DIGITS),
                    key_id=_fit(headers.get(H_RESPONSE_KEY_ID), 64, _TOKEN),
                    signature_hex=_fit(headers.get(H_RESPONSE_SIGNATURE), 128, _HEX),
                    signature_alg=f"{ALGORITHM}-RESPONSE",
                    signed_fields=SIGNED_RESPONSE_FIELDS,
                    verified=bool(authentic),
                    verifier_id=self.ctx.settings.service_name or "remitter",
                    verified_at=observed,
                    check_id=check_id,
                    latency_ms=latency_ms,
                )
            )
        return evidence_id

    # ------------------------------------------------------------------ payments
    def create_payment(self, req: PaymentRequest, simulate: str | None = None):
        m = self.ctx.metrics
        txn, replayed = self.ledger.create_payment(req, actor="REMITTER")
        m.inc("payments_replayed" if replayed else "payments_created", route=txn.route_id)
        if replayed or simulate == "hold":
            return self._reload(txn.transaction_id), replayed
        if simulate == "timeout":
            self.ledger.mark_ambiguous(
                txn.transaction_id,
                "simulated timeout: request never reached the switch",
                metadata={"ambiguity_cause": "TIMEOUT"},
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
        cause = None
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
                reason, cause = "response lost (simulated)", "LOST_RESPONSE"
            else:
                latency = (time.perf_counter() - started) * 1000
                evidence = self.evidence_from_response(
                    txn_id, resp, "/api/v1/pay", latency_ms=latency
                )
                if evidence is None:
                    reason, cause = f"switch HTTP {resp.status_code}", "HTTP_ERROR"
        except httpx.TimeoutException:
            reason, cause = "switch timeout", "TIMEOUT"
        except httpx.HTTPError as exc:
            reason, cause = f"switch unreachable: {type(exc).__name__}", "UNREACHABLE"
        m.observe_latency("pay_latency", (time.perf_counter() - started) * 1000, route=txn.route_id)
        if evidence is not None and not evidence.authenticated:
            reason, cause = "switch response failed signature verification", "UNVERIFIED_RESPONSE"
            evidence = None
        if evidence is not None and evidence.transaction_id != txn_id:
            reason, cause = "switch response names a different transaction", "WRONG_TRANSACTION"
            evidence = None
        if evidence is None or evidence.status in (SwitchStatus.PENDING, SwitchStatus.NOT_FOUND):
            m.inc("payments_ambiguous", route=txn.route_id)
            why = reason or f"switch answered {evidence.status if evidence else 'nothing'}"
            facts = {"ambiguity_cause": cause}
            if evidence is not None:  # a signed, inconclusive answer: keep its evidence
                facts = {
                    "ambiguity_cause": "PENDING_RESPONSE"
                    if evidence.status == SwitchStatus.PENDING
                    else "NOT_FOUND_RESPONSE",
                    "evidence_id": evidence.evidence_id,
                }
            return self.ledger.mark_ambiguous(txn_id, why, metadata=facts)
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
        check_id = txn.check_in_flight_id
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
            timeout = isinstance(exc, httpx.TimeoutException)
            res = self.ledger.apply_evidence(
                txn_id,
                None,
                actor="DISPATCHER",
                expected_version=expected_version,
                count_check=True,
                check_fact={
                    "kind": "TIMED_OUT",
                    "result_code": "CHECK_TIMEOUT" if timeout else "UPSTREAM_UNAVAILABLE",
                    "check_id": check_id,
                    "counted": True,
                    "transport_error": type(exc).__name__,
                },
            )
            return {
                "admitted": True,
                "status": None,
                "latency_ms": latency,
                "error": type(exc).__name__,
                "check_id": check_id,
                "result": res.model_dump(),
            }
        latency = (time.perf_counter() - started) * 1000
        if resp.status_code in (429, 403):
            body = resp.json()
            code = body.get("error_code", "REJECTED")
            res = self.ledger.check_not_admitted(
                txn_id,
                expected_version,
                code,
                check_fact={
                    "kind": "REJECTED",
                    "trackb_code": code,
                    "check_id": check_id,
                    "counted": False,
                },
            )
            return {
                "admitted": False,
                "status": None,
                "latency_ms": latency,
                "rejection_reason": body.get("error_code"),
                "check_id": check_id,
                "result": res.model_dump(),
            }
        evidence = self.evidence_from_response(
            txn_id,
            resp,
            "/api/v1/check-status",
            kind="STATUS_CHECK_RESPONSE",
            check_id=check_id,
            latency_ms=latency,
        )
        if evidence is not None and evidence.transaction_id != txn_id:
            evidence = None  # never apply an answer about a different transaction
        usable = evidence if evidence and evidence.authenticated else None
        if usable is None:
            fact = {
                "kind": "TIMED_OUT",
                "result_code": "UPSTREAM_UNAVAILABLE",
                "check_id": check_id,
                "counted": True,
                "note": "no verifiable answer"
                + (f" (HTTP {resp.status_code})" if evidence is None else " (bad signature)"),
            }
        elif usable.status in INCONCLUSIVE:
            fact = {
                "kind": "ANSWERED",
                "check_result": INCONCLUSIVE[usable.status],
                "check_id": check_id,
                "evidence_id": usable.evidence_id,
                "counted": True,
            }
        else:
            fact = {"kind": "ANSWERED", "check_id": check_id, "counted": True}
        res = self.ledger.apply_evidence(
            txn_id,
            usable,
            actor="DISPATCHER",
            expected_version=expected_version,
            count_check=True,
            check_fact=fact,
        )
        return {
            "admitted": True,
            # only a verified answer is reported as a status (also to controller.observe)
            "status": usable.status.value if usable else None,
            "unverified_status": evidence.status.value if evidence and not usable else None,
            "latency_ms": latency,
            "check_id": check_id,
            "evidence_id": evidence.evidence_id if evidence else None,
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
        evidence = self.evidence_from_response(txn_id, resp, path, kind="RECONCILIATION_RECORD")
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
