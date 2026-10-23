"""Authoritative ledger service: the ONLY code path that mutates money.

Every public method runs one PostgreSQL transaction that

1. locks the transaction row (``SELECT ... FOR UPDATE``),
2. re-validates version, state transition and evidence against that locked row,
3. inserts the financial operation (unique ``operation_id``, unique idempotency key,
   unique ``(transaction_id, operation_type, bank_id)``),
4. posts balanced ledger entries and updates balances (accounts locked in sorted order),
5. updates the transaction state/version and appends ``transaction_events``,
6. inserts outbox + audit rows,
7. commits.

Rejected operations are rolled back and then audited in a separate transaction.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.aorts.app.db import Database
from backend.aorts.app.logging_config import log_event
from backend.aorts.banking.ledger import invariants as inv
from backend.aorts.banking.ledger.invariants import SafetyViolation
from backend.aorts.banking.ledger.models import (
    Account,
    FinancialOperation,
    LedgerEntry,
    ProcessedEvent,
    Transaction,
    TransactionEvent,
)
from backend.aorts.banking.ledger.safety_gate import check_before_mutation
from backend.aorts.contracts.enums import (
    AccountType,
    Decision,
    ErrorCode,
    EventType,
    FinancialOutcome,
    OperationType,
    SwitchStatus,
    TxnState,
)
from backend.aorts.contracts.events import DomainEvent, new_id, new_transaction_id
from backend.aorts.contracts.requests import PaymentRequest, StatusEvidence
from backend.aorts.contracts.responses import EligibilityResponse, OperationResult
from backend.aorts.events.outbox import add_outbox
from backend.aorts.security.audit import append_records, make_record

logger = logging.getLogger("aorts.ledger")


def suspense_account(bank_id: str) -> str:
    return f"{bank_id}-SUSPENSE"


def settlement_account(bank_id: str) -> str:
    return f"{bank_id}-SETTLEMENT"


def request_hash(req: PaymentRequest) -> str:
    material = req.model_dump(exclude={"idempotency_key", "correlation_id"})
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


@dataclass
class _Ctx:
    """Per-operation scratchpad: audit rows are appended just before commit."""

    actor: str
    audit: list[dict] = field(default_factory=list)


class LedgerService:
    def __init__(
        self,
        db: Database,
        *,
        deadline_seconds: int = 120,
        auto_reverse: bool = True,
        backoff_base: float = 1.0,
        backoff_max: float = 30.0,
        max_checks: int = 6,
        rng: random.Random | None = None,
        experiment_id: str | None = None,
    ):
        self.db = db
        self.experiment_id = experiment_id  # v1.1 provenance; recorded on new transactions
        self.deadline_seconds = deadline_seconds
        self.auto_reverse = auto_reverse
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.max_checks = max_checks
        self.rng = rng or random.Random()

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    def _lock_txn(self, s: Session, txn_id: str) -> Transaction:
        txn = s.execute(
            select(Transaction).where(Transaction.transaction_id == txn_id).with_for_update()
        ).scalar()
        if txn is None:
            raise SafetyViolation(
                ErrorCode.TRANSACTION_NOT_FOUND, f"transaction {txn_id} not found", 404
            )
        return txn

    @staticmethod
    def _lock_accounts(s: Session, txn: Transaction) -> None:
        """Global lock order: transaction row -> every remitter account the operation may touch, sorted.

        Needed because re-updating a ``transactions`` row in the same DB transaction re-runs its FK check
        (KEY SHARE on the sender account). Taking the sender/suspense/settlement locks up front, in the same
        sorted order as ``create_payment``, makes lock-order cycles (deadlocks) impossible.
        """
        bank = txn.remitter_bank_id
        ids = sorted({txn.sender_account_id, suspense_account(bank), settlement_account(bank)})
        s.execute(
            select(Account.account_id)
            .where(Account.account_id.in_(ids))
            .order_by(Account.account_id)
            .with_for_update()
        )

    def _transition(
        self,
        s: Session,
        ctx: _Ctx,
        txn: Transaction,
        new_state: TxnState,
        *,
        reason: str,
        event_type: EventType,
        metadata: dict | None = None,
        outcome_override: FinancialOutcome | None = None,
        correlation_id: str | None = None,
    ) -> None:
        inv.assert_transition(txn.state, new_state)
        prev_state, prev_version = txn.state, txn.version
        workflow, outcome = inv.PROJECTION[new_state]
        now = self._now()
        txn.state = new_state.value
        txn.workflow_state = workflow.value
        if outcome_override is not None:
            txn.financial_outcome = outcome_override.value
        elif not (new_state == TxnState.RESOLVED_BY_RECONCILIATION):
            txn.financial_outcome = outcome.value
        txn.version = prev_version + 1
        txn.updated_at = now
        txn.last_reason = reason[:200]
        if new_state != TxnState.CHECK_IN_FLIGHT:
            txn.check_in_flight_id = None  # the check (if any) has ended
        if new_state == TxnState.AMBIGUOUS and txn.ambiguous_since is None:
            txn.ambiguous_since = now
        if new_state in inv.TERMINAL or new_state in (
            TxnState.CONFIRMED_SUCCESS,
            TxnState.CONFIRMED_FAILED,
        ):
            txn.resolved_at = txn.resolved_at or now
        event_id = new_id("evt")
        s.add(
            TransactionEvent(
                event_id=event_id,
                transaction_id=txn.transaction_id,
                event_type=event_type.value,
                previous_state=prev_state,
                new_state=new_state.value,
                previous_version=prev_version,
                new_version=txn.version,
                actor=ctx.actor,
                reason=reason[:200],
                metadata_json=metadata or {},
            )
        )
        add_outbox(
            s,
            DomainEvent(
                event_id=event_id,
                transaction_id=txn.transaction_id,
                route_id=txn.route_id,
                bank_id=txn.remitter_bank_id,
                amount_paise=txn.amount_paise,
                event_type=event_type.value,
                transaction_version=txn.version,
                correlation_id=correlation_id or txn.correlation_id,
                payload={
                    "previous_state": prev_state,
                    "last_known_state": new_state.value,
                    "financial_outcome": txn.financial_outcome,
                    "checks_used": txn.checks_used,
                    "reason": reason,
                    "deadline_at": txn.deadline_at.isoformat() if txn.deadline_at else None,
                    "ambiguous_since": txn.ambiguous_since.isoformat()
                    if txn.ambiguous_since
                    else None,
                    **(metadata or {}),
                },
            ),
        )
        ctx.audit.append(
            make_record(
                f"STATE_{new_state.value}",
                Decision.ACCEPTED,
                ctx.actor,
                reason=reason,
                transaction_id=txn.transaction_id,
                route_id=txn.route_id,
                correlation_id=txn.correlation_id,
                previous_version=prev_version,
                new_version=txn.version,
                metadata={"from": prev_state, "to": new_state.value, **(metadata or {})},
            )
        )

    def _post(
        self,
        s: Session,
        ctx: _Ctx,
        *,
        txn_id: str,
        bank_id: str,
        op_type: OperationType,
        op_id: str,
        amount: int,
        debit_account: str,
        credit_account: str,
        evidence: dict | None = None,
        references: str | None = None,
        route_id: str | None = None,
    ) -> FinancialOperation:
        """A. + D. Insert a unique operation and its balanced two-leg journal."""
        # Execution-time financial safety gate: authoritative re-read + evidence + uniqueness, right now.
        gate = check_before_mutation(
            s, txn_id=txn_id, bank_id=bank_id, op_type=op_type.value, op_id=op_id, evidence=evidence
        )
        op = FinancialOperation(
            operation_id=op_id,
            transaction_id=txn_id,
            bank_id=bank_id,
            operation_type=op_type.value,
            amount_paise=amount,
            idempotency_key=f"{op_type.value}:{bank_id}:{txn_id}",
            references_operation_id=references,
            result="APPLIED",
            actor=ctx.actor,
            evidence=evidence or {},
        )
        s.add(op)
        s.flush()  # unique constraints fire here -> IntegrityError on any duplicate
        # populate_existing: the account objects may already be in the session from an unlocked read
        # (create_payment loads the sender first); without it the funds check would use that stale
        # balance instead of the row just locked, and concurrent debits would hit the DB constraint.
        accounts = {
            a.account_id: a
            for a in s.execute(
                select(Account)
                .where(Account.account_id.in_(sorted({debit_account, credit_account})))
                .order_by(Account.account_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).scalars()
        }
        for acc_id in (debit_account, credit_account):
            if acc_id not in accounts:
                raise SafetyViolation(
                    ErrorCode.ACCOUNT_NOT_FOUND, f"account {acc_id} not found", 422
                )
        debit_acc = accounts[debit_account]
        if debit_acc.account_type == AccountType.CUSTOMER and debit_acc.balance_paise < amount:
            raise SafetyViolation(ErrorCode.INSUFFICIENT_FUNDS, "insufficient funds", 422)
        legs = [
            LedgerEntry(
                operation_id=op_id,
                transaction_id=txn_id,
                account_id=debit_account,
                direction="DEBIT",
                amount_paise=amount,
            ),
            LedgerEntry(
                operation_id=op_id,
                transaction_id=txn_id,
                account_id=credit_account,
                direction="CREDIT",
                amount_paise=amount,
            ),
        ]
        if not inv.journal_is_balanced(legs):
            raise SafetyViolation(ErrorCode.INTERNAL_ERROR, "unbalanced journal", 500)
        s.add_all(legs)
        now = self._now()
        s.execute(
            update(Account)
            .where(Account.account_id == debit_account)
            .values(
                balance_paise=Account.balance_paise - amount,
                version=Account.version + 1,
                updated_at=now,
            )
        )
        s.execute(
            update(Account)
            .where(Account.account_id == credit_account)
            .values(
                balance_paise=Account.balance_paise + amount,
                version=Account.version + 1,
                updated_at=now,
            )
        )
        ctx.audit.append(
            make_record(
                f"{op_type.value}_COMMITTED",
                Decision.ACCEPTED,
                ctx.actor,
                transaction_id=txn_id,
                route_id=route_id,
                metadata={
                    "operation_id": op_id,
                    "amount_paise": amount,
                    "debit": debit_account,
                    "credit": credit_account,
                    "safety_gate": gate,
                },
            )
        )
        return op

    def _audit_rejection(
        self,
        actor: str,
        action: str,
        txn_id: str | None,
        exc: SafetyViolation,
        route_id: str | None = None,
        metadata: dict | None = None,
        decision: Decision = Decision.REJECTED,
    ) -> None:
        try:
            with self.db.session() as s:
                append_records(
                    s,
                    [
                        make_record(
                            action,
                            decision,
                            actor,
                            reason=f"{exc.code}: {exc.message}",
                            transaction_id=txn_id,
                            route_id=route_id,
                            metadata={**exc.details, **(metadata or {})},
                        )
                    ],
                )
        except Exception:  # noqa: BLE001
            logger.exception("failed to audit rejection")
        log_event(
            logger,
            "operation rejected",
            logging.WARNING,
            action=action,
            transaction_id=txn_id,
            code=str(exc.code),
            reason=exc.message,
            actor=actor,
        )

    def _commit(self, s: Session, ctx: _Ctx) -> None:
        append_records(s, ctx.audit)

    def _backoff(self, checks_used: int) -> timedelta:
        delay = min(self.backoff_max, self.backoff_base * (2 ** max(0, checks_used - 1)))
        return timedelta(seconds=delay * (0.5 + self.rng.random() / 2))  # equal-jitter backoff

    # ------------------------------------------------------------------ payments
    def create_payment(
        self, req: PaymentRequest, *, actor: str = "REMITTER"
    ) -> tuple[Transaction, bool]:
        """Atomic debit. Returns (transaction, replayed). Same idempotency key -> same transaction."""
        req_hash = request_hash(req)
        existing = self._find_by_idempotency(req.idempotency_key, req_hash)
        if existing is not None:
            return existing, True
        ctx = _Ctx(actor)
        try:
            with self.db.session() as s:
                sender = s.execute(
                    select(Account).where(Account.account_id == req.sender_account_id)
                ).scalar()
                if sender is None or sender.account_type != AccountType.CUSTOMER:
                    raise SafetyViolation(
                        ErrorCode.ACCOUNT_NOT_FOUND, "sender account not found", 422
                    )
                bank = sender.bank_id
                txn_id = new_transaction_id(bank)
                # Lock every account this payment will touch, in sorted order, BEFORE inserting rows that
                # reference them: the transactions.sender_account_id FK takes a KEY SHARE lock, and two
                # concurrent payments from one sender would otherwise deadlock upgrading it to FOR UPDATE.
                s.execute(
                    select(Account.account_id)
                    .where(
                        Account.account_id.in_(sorted({sender.account_id, suspense_account(bank)}))
                    )
                    .order_by(Account.account_id)
                    .with_for_update()
                )
                now = self._now()
                deadline = now + timedelta(seconds=req.deadline_seconds or self.deadline_seconds)
                txn = Transaction(
                    transaction_id=txn_id,
                    sender_account_id=sender.account_id,
                    beneficiary_account_id=req.beneficiary_account_id,
                    remitter_bank_id=bank,
                    beneficiary_bank_id=req.beneficiary_bank_id,
                    route_id=f"{bank}-{req.beneficiary_bank_id}",
                    amount_paise=req.amount_paise,
                    currency=req.currency,
                    state=TxnState.DEBITED.value,
                    financial_outcome=FinancialOutcome.UNKNOWN.value,
                    workflow_state="ACTIVE",
                    version=1,
                    idempotency_key=req.idempotency_key,
                    request_hash=req_hash,
                    checks_used=0,
                    created_at=now,
                    updated_at=now,
                    deadline_at=deadline,
                    correlation_id=req.correlation_id,
                    experiment_id=self.experiment_id,
                )
                s.add(txn)
                s.flush()
                self._post(
                    s,
                    ctx,
                    txn_id=txn_id,
                    bank_id=bank,
                    op_type=OperationType.DEBIT,
                    op_id=f"{txn_id}:DEBIT",
                    amount=req.amount_paise,
                    debit_account=sender.account_id,
                    credit_account=suspense_account(bank),
                    route_id=txn.route_id,
                )
                debit_event_id = new_id("evt")  # one id for the history row and its outbox event
                s.add(
                    TransactionEvent(
                        event_id=debit_event_id,
                        transaction_id=txn_id,
                        event_type=EventType.PAYMENT_DEBITED.value,
                        previous_state=None,
                        new_state=TxnState.DEBITED.value,
                        previous_version=None,
                        new_version=1,
                        actor=actor,
                        reason="payment accepted and debited",
                        metadata_json={},
                    )
                )
                add_outbox(
                    s,
                    DomainEvent(
                        event_id=debit_event_id,
                        transaction_id=txn_id,
                        route_id=txn.route_id,
                        bank_id=bank,
                        amount_paise=req.amount_paise,
                        event_type=EventType.PAYMENT_DEBITED.value,
                        transaction_version=1,
                        correlation_id=req.correlation_id,
                        payload={
                            "last_known_state": "DEBITED",
                            "checks_used": 0,
                            "deadline_at": deadline.isoformat(),
                            "created_at": now.isoformat(),
                            "beneficiary_bank_id": req.beneficiary_bank_id,
                        },
                    ),
                )
                ctx.audit.append(
                    make_record(
                        "PAYMENT_ACCEPTED",
                        Decision.ACCEPTED,
                        actor,
                        transaction_id=txn_id,
                        route_id=txn.route_id,
                        correlation_id=req.correlation_id,
                        new_version=1,
                        metadata={"amount_paise": req.amount_paise},
                    )
                )
                self._commit(s, ctx)
            return txn, False
        except IntegrityError:
            # Concurrent request with the same idempotency key won the race.
            existing = self._find_by_idempotency(req.idempotency_key, req_hash)
            if existing is not None:
                return existing, True
            raise
        except SafetyViolation as exc:
            self._audit_rejection(
                actor,
                "PAYMENT_REJECTED",
                None,
                exc,
                metadata={"sender": req.sender_account_id, "amount_paise": req.amount_paise},
            )
            raise

    def _find_by_idempotency(self, key: str, req_hash: str) -> Transaction | None:
        with self.db.session() as s:
            txn = s.execute(select(Transaction).where(Transaction.idempotency_key == key)).scalar()
        if txn is None:
            return None
        if txn.request_hash != req_hash:
            raise SafetyViolation(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "idempotency key reused with a different request body",
                409,
            )
        return txn

    def mark_ambiguous(
        self, txn_id: str, reason: str, *, actor: str = "REMITTER", metadata: dict | None = None
    ) -> OperationResult:
        """A timeout/lost response: record uncertainty, never a failure (rule C)."""
        ctx = _Ctx(actor)
        with self.db.session() as s:
            txn = self._lock_txn(s, txn_id)
            if txn.state != TxnState.DEBITED:
                return self._result(txn, Decision.DEFERRED, f"already {txn.state}")
            prev = txn.version
            self._transition(
                s,
                ctx,
                txn,
                TxnState.AMBIGUOUS,
                reason=reason,
                event_type=EventType.PAYMENT_AMBIGUOUS,
                metadata=metadata,
            )
            txn.next_eligible_at = self._now() + self._backoff(1)
            self._commit(s, ctx)
            return self._result(txn, Decision.ACCEPTED, reason, prev=prev)

    # ------------------------------------------------------------------ evidence
    def apply_evidence(
        self,
        txn_id: str,
        evidence: StatusEvidence | None,
        *,
        actor: str,
        expected_version: int | None = None,
        event_id: str | None = None,
        count_check: bool = False,
        consumer: str = "remitter-evidence",
        check_fact: dict | None = None,
    ) -> OperationResult:
        """``check_fact`` records, at its source, what this status check returned (v1.1)."""
        """Apply an authenticated switch statement to a transaction (rules B, C, F, G, H, I)."""
        ctx = _Ctx(actor)
        try:
            with self.db.session() as s:
                if event_id is not None:
                    # Replay protection: an event id can be consumed once, across all replicas.
                    s.add(
                        ProcessedEvent(
                            consumer=consumer,
                            event_id=event_id,
                            transaction_id=txn_id,
                            result={"status": evidence.status if evidence else None},
                        )
                    )
                    try:
                        s.flush()
                    except IntegrityError as dup:
                        raise SafetyViolation(
                            ErrorCode.DUPLICATE_EVENT,
                            f"event {event_id} already processed",
                            409,
                            {"event_id": event_id},
                        ) from dup
                txn = self._lock_txn(s, txn_id)
                prev = txn.version
                inv.assert_version(txn.version, expected_version)
                check = inv.check_evidence(txn_id, evidence)
                if count_check and txn.state == TxnState.CHECK_IN_FLIGHT:
                    txn.checks_used += 1
                    txn.last_check_at = self._now()
                if not check.ok:
                    if check.code == ErrorCode.WRONG_TRANSACTION or (
                        evidence and not evidence.authenticated
                    ):
                        raise SafetyViolation(
                            check.code,
                            check.reason,
                            409 if check.code == ErrorCode.WRONG_TRANSACTION else 401,
                        )
                    # PENDING / NOT_FOUND / no answer: preserve uncertainty (rule I).
                    result = self._preserve_uncertainty(
                        s, ctx, txn, check.reason, metadata=check_fact
                    )
                    self._commit(s, ctx)
                    return result
                status = evidence.status
                state = TxnState(txn.state)
                if state in inv.TERMINAL or state in (
                    TxnState.CONFIRMED_SUCCESS,
                    TxnState.CONFIRMED_FAILED,
                    TxnState.REVERSAL_INITIATED,
                ):
                    agrees = (status == SwitchStatus.SUCCESS) == (
                        txn.financial_outcome == FinancialOutcome.SUCCESS
                    )
                    if agrees:
                        ctx.audit.append(
                            make_record(
                                "EVIDENCE_DUPLICATE",
                                Decision.DUPLICATE,
                                actor,
                                transaction_id=txn_id,
                                route_id=txn.route_id,
                                reason="outcome already applied; no financial effect",
                                previous_version=prev,
                                new_version=txn.version,
                            )
                        )
                        self._commit(s, ctx)
                        return self._result(
                            txn,
                            Decision.DUPLICATE,
                            "outcome already applied",
                            prev=prev,
                            duplicate=True,
                        )
                    raise SafetyViolation(
                        ErrorCode.ILLEGAL_TRANSITION,
                        f"conflicting evidence {status} for transaction already {txn.state}",
                        409,
                        {"state": txn.state, "evidence": str(status)},
                    )
                if state == TxnState.QUARANTINED:
                    raise SafetyViolation(
                        ErrorCode.ILLEGAL_TRANSITION,
                        "transaction is quarantined; manual release required",
                        409,
                    )
                ev = evidence.model_dump(mode="json")
                provenance = {"evidence_id": evidence.evidence_id}
                if check_fact and check_fact.get("check_id"):
                    provenance["check_id"] = check_fact["check_id"]
                self._lock_accounts(s, txn)
                if state == TxnState.HANDOFF_UDIR:
                    self._reconcile(s, ctx, txn, status, ev)
                elif status == SwitchStatus.SUCCESS:
                    self._transition(
                        s,
                        ctx,
                        txn,
                        TxnState.CONFIRMED_SUCCESS,
                        reason="authoritative SUCCESS",
                        event_type=EventType.PAYMENT_SUCCEEDED,
                        metadata={"evidence_source": evidence.source, **provenance},
                    )
                    txn.switch_reference = evidence.switch_reference
                    self._settle(s, ctx, txn, ev)
                else:
                    self._transition(
                        s,
                        ctx,
                        txn,
                        TxnState.CONFIRMED_FAILED,
                        reason="authoritative FAILED",
                        event_type=EventType.PAYMENT_FAILED,
                        metadata={
                            "evidence_source": evidence.source,
                            "why": evidence.reason,
                            **provenance,
                        },
                    )
                    txn.switch_reference = evidence.switch_reference
                    if self.auto_reverse:
                        self._reverse_locked(
                            s, ctx, txn, f"{txn.transaction_id}:REVERSAL", "auto-reversal", ev
                        )
                self._commit(s, ctx)
                return self._result(txn, Decision.ACCEPTED, f"applied {status}", prev=prev)
        except SafetyViolation as exc:
            self._audit_rejection(
                actor,
                "EVIDENCE_REJECTED"
                if exc.code != ErrorCode.DUPLICATE_EVENT
                else "DUPLICATE_EVENT_REJECTED",
                txn_id,
                exc,
                metadata={"event_id": event_id, "expected_version": expected_version},
            )
            raise

    def _preserve_uncertainty(
        self, s: Session, ctx: _Ctx, txn: Transaction, reason: str, metadata: dict | None = None
    ) -> OperationResult:
        prev = txn.version
        if txn.state == TxnState.CHECK_IN_FLIGHT:
            fact = metadata or {
                "kind": "TIMED_OUT",
                "result_code": "CHECK_TIMEOUT",
                "check_id": txn.check_in_flight_id,
                "note": "no answer recorded (crash recovery of a stuck check)",
            }
            self._transition(
                s,
                ctx,
                txn,
                TxnState.AMBIGUOUS,
                reason=f"check inconclusive: {reason}",
                event_type=EventType.CHECK_RESULT,
                metadata={"check_fact": fact},
            )
            txn.next_eligible_at = self._now() + self._backoff(txn.checks_used)
            if txn.checks_used >= self.max_checks:
                self._transition(
                    s,
                    ctx,
                    txn,
                    TxnState.HANDOFF_UDIR,
                    reason="check allowance exhausted",
                    event_type=EventType.ESCALATED,
                )
        elif txn.state == TxnState.DEBITED:
            self._transition(
                s,
                ctx,
                txn,
                TxnState.AMBIGUOUS,
                reason=reason,
                event_type=EventType.PAYMENT_AMBIGUOUS,
                metadata=metadata,
            )
            txn.next_eligible_at = self._now() + self._backoff(1)
        return self._result(txn, Decision.DEFERRED, reason, prev=prev)

    def _settle(self, s: Session, ctx: _Ctx, txn: Transaction, evidence: dict) -> None:
        self._post(
            s,
            ctx,
            txn_id=txn.transaction_id,
            bank_id=txn.remitter_bank_id,
            op_type=OperationType.SETTLE,
            op_id=f"{txn.transaction_id}:SETTLE",
            amount=txn.amount_paise,
            debit_account=suspense_account(txn.remitter_bank_id),
            credit_account=settlement_account(txn.remitter_bank_id),
            evidence=evidence,
            references=f"{txn.transaction_id}:DEBIT",
            route_id=txn.route_id,
        )
        self._transition(
            s,
            ctx,
            txn,
            TxnState.CLOSED,
            reason="settled",
            event_type=EventType.PAYMENT_CLOSED,
            # v1.1 provenance: the verified evidence that justified the settlement
            metadata={"evidence_id": (evidence or {}).get("evidence_id")},
        )

    def _reverse_locked(
        self, s: Session, ctx: _Ctx, txn: Transaction, op_id: str, reason: str, evidence: dict
    ) -> None:
        """F. A reversal references the original DEBIT of the same transaction."""
        debit = s.get(FinancialOperation, f"{txn.transaction_id}:DEBIT")
        if (
            debit is None
            or debit.transaction_id != txn.transaction_id
            or debit.operation_type != "DEBIT"
        ):
            raise SafetyViolation(
                ErrorCode.WRONG_TRANSACTION, "original debit not found for reversal", 409
            )
        self._transition(
            s,
            ctx,
            txn,
            TxnState.REVERSAL_INITIATED,
            reason=reason,
            event_type=EventType.CHECK_RESULT,
            metadata={"operation_id": op_id},
        )
        self._post(
            s,
            ctx,
            txn_id=txn.transaction_id,
            bank_id=txn.remitter_bank_id,
            op_type=OperationType.REVERSAL,
            op_id=op_id,
            amount=debit.amount_paise,
            debit_account=suspense_account(txn.remitter_bank_id),
            credit_account=txn.sender_account_id,
            evidence=evidence,
            references=debit.operation_id,
            route_id=txn.route_id,
        )
        self._transition(
            s,
            ctx,
            txn,
            TxnState.REVERSED,
            reason="reversal posted",
            event_type=EventType.REVERSAL_COMPLETED,
            metadata={"operation_id": op_id, "evidence_id": (evidence or {}).get("evidence_id")},
        )

    def _reconcile(
        self, s: Session, ctx: _Ctx, txn: Transaction, status: SwitchStatus, evidence: dict
    ) -> None:
        if status == SwitchStatus.SUCCESS:
            self._post(
                s,
                ctx,
                txn_id=txn.transaction_id,
                bank_id=txn.remitter_bank_id,
                op_type=OperationType.SETTLE,
                op_id=f"{txn.transaction_id}:SETTLE",
                amount=txn.amount_paise,
                debit_account=suspense_account(txn.remitter_bank_id),
                credit_account=settlement_account(txn.remitter_bank_id),
                evidence=evidence,
                references=f"{txn.transaction_id}:DEBIT",
                route_id=txn.route_id,
            )
            outcome = FinancialOutcome.SUCCESS
        else:
            self._post(
                s,
                ctx,
                txn_id=txn.transaction_id,
                bank_id=txn.remitter_bank_id,
                op_type=OperationType.REVERSAL,
                op_id=f"{txn.transaction_id}:REVERSAL",
                amount=txn.amount_paise,
                debit_account=suspense_account(txn.remitter_bank_id),
                credit_account=txn.sender_account_id,
                evidence=evidence,
                references=f"{txn.transaction_id}:DEBIT",
                route_id=txn.route_id,
            )
            outcome = FinancialOutcome.REVERSED
        self._transition(
            s,
            ctx,
            txn,
            TxnState.RESOLVED_BY_RECONCILIATION,
            reason=f"reconciled as {status}",
            event_type=EventType.PAYMENT_CLOSED,
            outcome_override=outcome,
            metadata={"evidence_id": (evidence or {}).get("evidence_id")},
        )

    # ------------------------------------------------------------------ explicit reversal
    def reverse(
        self, txn_id: str, operation_id: str, *, expected_version: int, actor: str, reason: str
    ) -> OperationResult:
        """Explicit (e.g. controller-requested) reversal. Exactly-once under concurrency."""
        ctx = _Ctx(actor)
        try:
            with self.db.session() as s:
                existing = s.get(FinancialOperation, operation_id)
                if existing is not None:
                    if (
                        existing.transaction_id != txn_id
                        or existing.operation_type != OperationType.REVERSAL
                    ):
                        raise SafetyViolation(
                            ErrorCode.WRONG_TRANSACTION,
                            "operation id already used for a different operation",
                            409,
                        )
                txn = self._lock_txn(s, txn_id)
                prev = txn.version
                if existing is None:  # re-read after acquiring the row lock (TOCTOU guard)
                    existing = s.get(FinancialOperation, operation_id, populate_existing=True)
                if existing is not None and existing.transaction_id == txn_id:
                    ctx.audit.append(
                        make_record(
                            "REVERSAL_DUPLICATE",
                            Decision.DUPLICATE,
                            actor,
                            transaction_id=txn_id,
                            route_id=txn.route_id,
                            reason="operation already applied; no second posting",
                            metadata={"operation_id": operation_id},
                        )
                    )
                    self._commit(s, ctx)
                    return self._result(
                        txn,
                        Decision.DUPLICATE,
                        "operation already applied",
                        prev=prev,
                        duplicate=True,
                        op_id=operation_id,
                    )
                if txn.state == TxnState.REVERSED:
                    raise SafetyViolation(
                        ErrorCode.ALREADY_APPLIED, "transaction already reversed", 409
                    )
                if txn.state != TxnState.CONFIRMED_FAILED:
                    # C. No authoritative failure evidence recorded -> no reversal (timeouts never qualify).
                    raise SafetyViolation(
                        ErrorCode.INSUFFICIENT_EVIDENCE,
                        f"reversal requires CONFIRMED_FAILED, transaction is {txn.state}",
                        409,
                        {"state": txn.state},
                    )
                inv.assert_version(txn.version, expected_version)
                self._lock_accounts(s, txn)
                self._reverse_locked(s, ctx, txn, operation_id, reason, {"requested_by": actor})
                self._commit(s, ctx)
                return self._result(
                    txn, Decision.ACCEPTED, "reversed", prev=prev, op_id=operation_id
                )
        except IntegrityError:
            # Unique constraint backstop: a concurrent identical reversal committed first.
            with self.db.session() as s:
                txn = s.get(Transaction, txn_id)
                op = s.get(FinancialOperation, operation_id)
            if op is not None and op.transaction_id == txn_id:
                return self._result(
                    txn,
                    Decision.DUPLICATE,
                    "operation already applied (concurrent)",
                    duplicate=True,
                    op_id=operation_id,
                )
            exc = SafetyViolation(
                ErrorCode.ALREADY_APPLIED, "transaction already reversed by another operation", 409
            )
            self._audit_rejection(
                actor, "REVERSAL_REJECTED", txn_id, exc, metadata={"operation_id": operation_id}
            )
            raise exc from None
        except SafetyViolation as exc:
            self._audit_rejection(
                actor, "REVERSAL_REJECTED", txn_id, exc, metadata={"operation_id": operation_id}
            )
            raise

    # ------------------------------------------------------------------ beneficiary bank books
    @staticmethod
    def _claim_consumer(bank_id: str) -> str:
        return f"beneficiary:{bank_id}"

    def _existing_claim(self, bank_id: str, txn_id: str) -> dict | None:
        with self.db.session() as s:
            row = s.get(ProcessedEvent, (self._claim_consumer(bank_id), txn_id))
            return dict(row.result) if row else None

    @staticmethod
    def _claim_status(claim: dict) -> tuple[SwitchStatus, str | None]:
        kind = claim.get("kind")
        if kind == "CREDIT":
            return SwitchStatus.SUCCESS, None
        if kind == "FENCE":
            return SwitchStatus.FAILED, "FENCED_NOT_CREDITED"
        return SwitchStatus.FAILED, claim.get("reason")

    def credit_beneficiary(
        self,
        *,
        bank_id: str,
        txn_id: str,
        operation_id: str,
        account_id: str,
        amount: int,
        route_id: str,
        actor: str,
    ) -> tuple[SwitchStatus, str | None, bool]:
        """Exactly-once credit on the beneficiary's books. Returns (status, reason, duplicate).

        The first writer of the ``(beneficiary:<bank>, txn_id)`` claim row wins: a CREDIT
        claim, a FAILED claim (unknown account) or a FENCE claim made by a status query.
        """
        if operation_id != f"{txn_id}:CREDIT":
            raise SafetyViolation(
                ErrorCode.WRONG_TRANSACTION,
                "credit operation id must reference its transaction",
                422,
            )
        ctx = _Ctx(actor)
        try:
            with self.db.session() as s:
                acc = s.get(Account, account_id)
                valid = (
                    acc is not None
                    and acc.bank_id == bank_id
                    and acc.account_type == AccountType.CUSTOMER
                )
                claim = (
                    {"kind": "CREDIT"}
                    if valid
                    else {"kind": "FAILED", "reason": "ACCOUNT_NOT_FOUND"}
                )
                s.add(
                    ProcessedEvent(
                        consumer=self._claim_consumer(bank_id),
                        event_id=txn_id,
                        transaction_id=txn_id,
                        result=claim,
                    )
                )
                s.flush()
                if valid:
                    self._post(
                        s,
                        ctx,
                        txn_id=txn_id,
                        bank_id=bank_id,
                        op_type=OperationType.CREDIT,
                        op_id=f"{operation_id}:{bank_id}",
                        amount=amount,
                        debit_account=settlement_account(bank_id),
                        credit_account=account_id,
                        route_id=route_id,
                    )
                else:
                    ctx.audit.append(
                        make_record(
                            "CREDIT_DECLINED",
                            Decision.REJECTED,
                            actor,
                            transaction_id=txn_id,
                            route_id=route_id,
                            reason="ACCOUNT_NOT_FOUND",
                        )
                    )
                self._commit(s, ctx)
            status, reason = self._claim_status(claim)
            return status, reason, False
        except IntegrityError:
            claim = self._existing_claim(bank_id, txn_id)
            if claim is None:
                raise
            status, reason = self._claim_status(claim)
            return status, reason, True

    def fence_beneficiary(
        self, *, bank_id: str, txn_id: str, actor: str
    ) -> tuple[SwitchStatus, str | None]:
        """Make "not credited" permanent: any later credit for this transaction is refused."""
        try:
            with self.db.session() as s:
                s.add(
                    ProcessedEvent(
                        consumer=self._claim_consumer(bank_id),
                        event_id=txn_id,
                        transaction_id=txn_id,
                        result={"kind": "FENCE"},
                    )
                )
                s.flush()
                append_records(
                    s,
                    [
                        make_record(
                            "BENEFICIARY_FENCED",
                            Decision.ACCEPTED,
                            actor,
                            transaction_id=txn_id,
                            reason="no credit recorded after forwarding finished",
                        )
                    ],
                )
            return SwitchStatus.FAILED, "FENCED_NOT_CREDITED"
        except IntegrityError:
            claim = self._existing_claim(bank_id, txn_id)
            return self._claim_status(claim) if claim else (SwitchStatus.PENDING, "claim race")

    def beneficiary_status(self, *, bank_id: str, txn_id: str) -> tuple[SwitchStatus, str | None]:
        claim = self._existing_claim(bank_id, txn_id)
        if claim is None:
            return SwitchStatus.NOT_FOUND, None
        return self._claim_status(claim)

    # ------------------------------------------------------------------ dispatcher hooks
    def start_check(
        self, txn_id: str, expected_version: int, *, actor: str = "DISPATCHER"
    ) -> OperationResult:
        ctx = _Ctx(actor)
        with self.db.session() as s:
            txn = self._lock_txn(s, txn_id)
            prev = txn.version
            inv.assert_version(txn.version, expected_version)
            check_id = new_id("chk")  # genuine identity of this dispatch attempt
            self._transition(
                s,
                ctx,
                txn,
                TxnState.CHECK_IN_FLIGHT,
                reason="status check dispatched",
                event_type=EventType.CHECK_STARTED,
                metadata={"check_id": check_id},
            )
            txn.check_in_flight_id = check_id
            self._commit(s, ctx)
            return self._result(txn, Decision.ACCEPTED, "check in flight", prev=prev)

    def check_not_admitted(
        self,
        txn_id: str,
        expected_version: int,
        reason: str,
        *,
        actor: str = "DISPATCHER",
        check_fact: dict | None = None,
    ) -> OperationResult:
        """Switch refused the check (rate limit / credits): return to AMBIGUOUS WITHOUT using an allowance."""
        ctx = _Ctx(actor)
        with self.db.session() as s:
            txn = self._lock_txn(s, txn_id)
            prev = txn.version
            inv.assert_version(txn.version, expected_version)
            self._transition(
                s,
                ctx,
                txn,
                TxnState.AMBIGUOUS,
                reason=f"check not admitted: {reason}",
                event_type=EventType.CHECK_RESULT,
                metadata={
                    "check_fact": check_fact
                    or {
                        "kind": "REJECTED",
                        "trackb_code": reason,
                        "check_id": txn.check_in_flight_id,
                    }
                },
            )
            txn.next_eligible_at = self._now() + self._backoff(max(1, txn.checks_used))
            self._commit(s, ctx)
            return self._result(txn, Decision.DEFERRED, reason, prev=prev)

    def escalate(
        self,
        txn_id: str,
        reason: str,
        *,
        actor: str = "DISPATCHER",
        expected_version: int | None = None,
    ) -> OperationResult:
        ctx = _Ctx(actor)
        with self.db.session() as s:
            txn = self._lock_txn(s, txn_id)
            prev = txn.version
            inv.assert_version(txn.version, expected_version)
            self._transition(
                s, ctx, txn, TxnState.HANDOFF_UDIR, reason=reason, event_type=EventType.ESCALATED
            )
            self._commit(s, ctx)
            return self._result(txn, Decision.ACCEPTED, reason, prev=prev)

    def quarantine(self, txn_id: str, reason: str, *, actor: str) -> OperationResult:
        ctx = _Ctx(actor)
        with self.db.session() as s:
            txn = self._lock_txn(s, txn_id)
            prev = txn.version
            self._transition(
                s, ctx, txn, TxnState.QUARANTINED, reason=reason, event_type=EventType.QUARANTINED
            )
            self._commit(s, ctx)
            return self._result(txn, Decision.ACCEPTED, reason, prev=prev)

    def release_quarantine(
        self,
        txn_id: str,
        reason: str,
        *,
        actor: str,
        expected_version: int | None = None,
        escalate: bool = False,
    ) -> OperationResult:
        """Manual decision after review: back to AMBIGUOUS (automation resumes) or to HANDOFF_UDIR."""
        ctx = _Ctx(actor)
        with self.db.session() as s:
            txn = self._lock_txn(s, txn_id)
            prev = txn.version
            inv.assert_version(txn.version, expected_version)
            if txn.state != TxnState.QUARANTINED:
                raise SafetyViolation(
                    ErrorCode.ILLEGAL_TRANSITION, f"transaction is {txn.state}, not QUARANTINED"
                )
            target = TxnState.HANDOFF_UDIR if escalate else TxnState.AMBIGUOUS
            self._transition(
                s,
                ctx,
                txn,
                target,
                reason=f"quarantine released: {reason}",
                event_type=EventType.ESCALATED if escalate else EventType.CHECK_RESULT,
            )
            if target == TxnState.AMBIGUOUS:
                txn.next_eligible_at = self._now()
            self._commit(s, ctx)
            return self._result(txn, Decision.ACCEPTED, reason, prev=prev)

    def eligibility(
        self, txn_id: str, action: str, expected_version: int | None = None
    ) -> EligibilityResponse:
        """Read-only safety check Track A can call before proposing an action."""
        with self.db.session() as s:
            txn = s.get(Transaction, txn_id)
        if txn is None:
            return EligibilityResponse(
                transaction_id=txn_id,
                proposed_action=action,
                eligible=False,
                reasons=["TRANSACTION_NOT_FOUND"],
            )
        reasons: list[str] = []
        if expected_version is not None and expected_version != txn.version:
            reasons.append(f"STALE_VERSION: current {txn.version}")
        if action == "STATUS_CHECK":
            if txn.state != TxnState.AMBIGUOUS:
                reasons.append(f"state {txn.state} is not AMBIGUOUS")
            if txn.checks_used >= self.max_checks:
                reasons.append("check allowance exhausted")
            if txn.next_eligible_at and txn.next_eligible_at > self._now():
                reasons.append("backoff not elapsed")
        elif action == "REVERSAL":
            if txn.state != TxnState.CONFIRMED_FAILED:
                reasons.append(
                    f"INSUFFICIENT_EVIDENCE: reversal needs CONFIRMED_FAILED, state is {txn.state}"
                )
        elif action == "SETTLE":
            reasons.append(
                "settlement is executed only by the ledger on authoritative SUCCESS evidence"
            )
        elif action == "ESCALATE":
            if not inv.is_legal(txn.state, TxnState.HANDOFF_UDIR):
                reasons.append(f"cannot escalate from {txn.state}")
        return EligibilityResponse(
            transaction_id=txn_id,
            proposed_action=action,
            eligible=not reasons,
            reasons=reasons,
            current_state=txn.state,
            current_version=txn.version,
        )

    # ------------------------------------------------------------------ verification
    def verify_ledger(self) -> dict:
        """D/E/A checks over the whole ledger. Used by tests, experiments and the dashboard."""
        with self.db.session() as s:
            unbalanced = (
                s.execute(
                    select(LedgerEntry.operation_id)
                    .group_by(LedgerEntry.operation_id)
                    .having(
                        func.sum(
                            case(
                                (LedgerEntry.direction == "DEBIT", LedgerEntry.amount_paise),
                                else_=0,
                            )
                        )
                        != func.sum(
                            case(
                                (LedgerEntry.direction == "CREDIT", LedgerEntry.amount_paise),
                                else_=0,
                            )
                        )
                    )
                )
                .scalars()
                .all()
            )
            dup_ops = s.execute(
                select(FinancialOperation.transaction_id, FinancialOperation.operation_type)
                .group_by(
                    FinancialOperation.transaction_id,
                    FinancialOperation.operation_type,
                    FinancialOperation.bank_id,
                )
                .having(func.count() > 1)
            ).all()
            entry_net = dict(
                s.execute(
                    select(
                        LedgerEntry.account_id,
                        func.sum(
                            case(
                                (LedgerEntry.direction == "CREDIT", LedgerEntry.amount_paise),
                                else_=-LedgerEntry.amount_paise,
                            )
                        ),
                    ).group_by(LedgerEntry.account_id)
                ).all()
            )
            mismatched = []
            for acc in s.execute(select(Account)).scalars():
                if acc.balance_paise != acc.opening_balance_paise + int(
                    entry_net.get(acc.account_id, 0)
                ):
                    mismatched.append(acc.account_id)
            # E. Per-transaction conservation for the sender.
            sender_net = dict(
                s.execute(
                    select(
                        LedgerEntry.transaction_id,
                        func.sum(
                            case(
                                (LedgerEntry.direction == "CREDIT", LedgerEntry.amount_paise),
                                else_=-LedgerEntry.amount_paise,
                            )
                        ),
                    )
                    .join(Transaction, Transaction.transaction_id == LedgerEntry.transaction_id)
                    .where(LedgerEntry.account_id == Transaction.sender_account_id)
                    .group_by(LedgerEntry.transaction_id)
                ).all()
            )
            conservation_violations = []
            for txn in s.execute(select(Transaction)).scalars():
                expected = inv.expected_sender_net(txn.state, txn.amount_paise)
                if txn.state == TxnState.RESOLVED_BY_RECONCILIATION:
                    expected = (
                        0
                        if txn.financial_outcome == FinancialOutcome.REVERSED
                        else -txn.amount_paise
                    )
                if expected is not None and int(sender_net.get(txn.transaction_id, 0)) != expected:
                    conservation_violations.append(txn.transaction_id)
        return {
            "ok": not (unbalanced or dup_ops or mismatched or conservation_violations),
            "unbalanced_operations": list(unbalanced),
            "duplicate_operations": [list(r) for r in dup_ops],
            "balance_mismatches": mismatched,
            "conservation_violations": conservation_violations,
        }

    # ------------------------------------------------------------------ misc
    @staticmethod
    def _result(
        txn: Transaction,
        decision: Decision,
        reason: str,
        *,
        prev: int | None = None,
        duplicate: bool = False,
        op_id: str | None = None,
    ) -> OperationResult:
        return OperationResult(
            transaction_id=txn.transaction_id,
            operation_id=op_id,
            decision=decision.value,
            reason=reason,
            duplicate=duplicate,
            previous_version=prev,
            new_version=txn.version,
            state=txn.state,
            financial_outcome=txn.financial_outcome,
        )
