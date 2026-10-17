"""Simulated beneficiary bank (one process per bank: B, C, ...)."""

from __future__ import annotations

import time

from backend.aorts.app.dependencies import ServiceContext
from backend.aorts.banking.ledger.service import LedgerService
from backend.aorts.contracts.enums import SwitchStatus
from backend.aorts.contracts.requests import BeneficiaryCreditRequest
from backend.aorts.contracts.responses import BeneficiaryCreditResponse


class BeneficiaryUnavailable(Exception):
    pass


class BeneficiaryError(Exception):
    pass


class ResponseLost(Exception):
    pass


class BeneficiaryService:
    def __init__(self, ctx: ServiceContext, ledger: LedgerService):
        self.ctx = ctx
        self.ledger = ledger
        self.bank_id = ctx.settings.bank_id

    def credit(self, req: BeneficiaryCreditRequest) -> BeneficiaryCreditResponse:
        m = self.ctx.metrics
        m.inc("credit_received")
        fault = self.ctx.faults.decide(
            f"beneficiary:{self.bank_id}",
            stream="credit",
            key=f"{req.beneficiary_account_id}:{req.amount_paise}",
        )
        if fault.unavailable:
            m.inc("credit_unavailable")
            raise BeneficiaryUnavailable()
        started = time.perf_counter()
        self.ctx.faults.sleep(fault)  # slow bank: delay BEFORE processing (fencing makes this safe)
        if fault.error:
            m.inc("credit_error")
            raise BeneficiaryError()
        status, reason, duplicate = self.ledger.credit_beneficiary(
            bank_id=self.bank_id,
            txn_id=req.transaction_id,
            operation_id=req.operation_id,
            account_id=req.beneficiary_account_id,
            amount=req.amount_paise,
            route_id=req.route_id,
            actor=self.bank_id,
        )
        m.inc("credit_processed", status=status.value, duplicate=str(duplicate))
        m.observe_latency("credit_latency", (time.perf_counter() - started) * 1000)
        if fault.drop_response:
            m.inc("credit_response_dropped")
            raise ResponseLost()
        return BeneficiaryCreditResponse(
            transaction_id=req.transaction_id,
            operation_id=req.operation_id,
            status=status,
            reason=reason,
            duplicate=duplicate,
        )

    def _query_fault(self) -> None:
        """A degraded bank is also slow / failing for status queries, not only for credits."""
        fault = self.ctx.faults.decide(f"beneficiary:{self.bank_id}", stream="query")
        if fault.unavailable:
            self.ctx.metrics.inc("query_unavailable")
            raise BeneficiaryUnavailable()
        self.ctx.faults.sleep(fault)
        if fault.error:
            self.ctx.metrics.inc("query_error")
            raise BeneficiaryError()

    def status(self, txn_id: str) -> tuple[SwitchStatus, str | None]:
        self._query_fault()
        return self.ledger.beneficiary_status(bank_id=self.bank_id, txn_id=txn_id)

    def fence(self, txn_id: str) -> tuple[SwitchStatus, str | None]:
        self._query_fault()
        self.ctx.metrics.inc("fence_requests")
        return self.ledger.fence_beneficiary(
            bank_id=self.bank_id, txn_id=txn_id, actor=self.bank_id
        )
