"""Loan origination and servicing operations.

Covers the lifecycle Fineract enforces:
    submit (application) -> approve -> disburse -> repay

Disbursement and repayment on a cash-accounting product generate the
corresponding loan-portfolio and interest-income journal entries in Fineract.
"""

from __future__ import annotations

import random
from datetime import date
from typing import Any

from ..bootstrap import BankingSetup
from ..client import FineractClient
from ..config import DATE_FORMAT, LOCALE
from .clients import fineract_date

# Loan product terms, mirrored from bootstrap so the application is consistent
# with the product it is submitted against.
NUMBER_OF_REPAYMENTS = 12
REPAYMENT_EVERY = 1
REPAYMENT_FREQUENCY_TYPE = 2   # months
INTEREST_RATE_PER_PERIOD = 12
AMORTIZATION_TYPE = 1          # equal installments
INTEREST_TYPE = 0              # declining balance
INTEREST_CALCULATION_PERIOD_TYPE = 1


def apply_for_loan(
    client: FineractClient,
    setup: BankingSetup,
    client_id: int,
    principal: float,
    business_date: date,
) -> int | None:
    """Submit a loan application. Returns the loan id."""
    on_date = fineract_date(business_date)
    body: dict[str, Any] = {
        "clientId": client_id,
        "productId": setup.loan_product_id,
        "loanType": "individual",
        "principal": principal,
        "loanTermFrequency": NUMBER_OF_REPAYMENTS,
        "loanTermFrequencyType": REPAYMENT_FREQUENCY_TYPE,
        "numberOfRepayments": NUMBER_OF_REPAYMENTS,
        "repaymentEvery": REPAYMENT_EVERY,
        "repaymentFrequencyType": REPAYMENT_FREQUENCY_TYPE,
        "interestRatePerPeriod": INTEREST_RATE_PER_PERIOD,
        "amortizationType": AMORTIZATION_TYPE,
        "interestType": INTEREST_TYPE,
        "interestCalculationPeriodType": INTEREST_CALCULATION_PERIOD_TYPE,
        "expectedDisbursementDate": on_date,
        "submittedOnDate": on_date,
        "dateFormat": DATE_FORMAT,
        "locale": LOCALE,
    }
    body.update(setup.loan_strategy)

    result, payload = client.post(
        "/loans",
        category="loan",
        operation="loan.apply",
        json_body=body,
        context={"clientId": client_id, "principal": principal},
    )
    if not result.ok:
        return None
    return int(payload["loanId"]) if "loanId" in payload else result.resource_id


def approve_loan(
    client: FineractClient,
    loan_id: int,
    principal: float,
    business_date: date,
) -> bool:
    result, _ = client.post(
        f"/loans/{loan_id}",
        category="loan",
        operation="loan.approve",
        params={"command": "approve"},
        json_body={
            "approvedOnDate": fineract_date(business_date),
            "approvedLoanAmount": principal,
            "dateFormat": DATE_FORMAT,
            "locale": LOCALE,
        },
        context={"loanId": loan_id},
    )
    return result.ok


def disburse_loan(
    client: FineractClient,
    loan_id: int,
    principal: float,
    business_date: date,
) -> bool:
    result, _ = client.post(
        f"/loans/{loan_id}",
        category="loan",
        operation="loan.disburse",
        params={"command": "disburse"},
        json_body={
            "actualDisbursementDate": fineract_date(business_date),
            "transactionAmount": principal,
            "dateFormat": DATE_FORMAT,
            "locale": LOCALE,
        },
        context={"loanId": loan_id},
    )
    return result.ok


def repay_loan(
    client: FineractClient,
    loan_id: int,
    amount: float,
    business_date: date,
    payment_type_id: int | None = None,
) -> bool:
    """Post a loan repayment transaction."""
    body = {
        "transactionDate": fineract_date(business_date),
        "transactionAmount": amount,
        "dateFormat": DATE_FORMAT,
        "locale": LOCALE,
    }
    if payment_type_id is not None:
        body["paymentTypeId"] = payment_type_id

    result, _ = client.post(
        f"/loans/{loan_id}/transactions",
        category="transaction",
        operation="loan.repayment",
        params={"command": "repayment"},
        json_body=body,
        context={"loanId": loan_id, "amount": amount},
    )
    return result.ok


def read_loan(client: FineractClient, loan_id: int) -> dict[str, Any] | None:
    result, payload = client.get(
        f"/loans/{loan_id}",
        category="loan",
        operation="loan.read",
        context={"loanId": loan_id},
    )
    return payload if result.ok and isinstance(payload, dict) else None


def originate_loan(
    client: FineractClient,
    setup: BankingSetup,
    client_id: int,
    rng: random.Random,
    business_date: date,
) -> tuple[int, float] | None:
    """Run a full application -> approval -> disbursement. Returns (loan_id, principal)."""
    principal = float(rng.randrange(10_000, 200_000, 5_000))

    loan_id = apply_for_loan(client, setup, client_id, principal, business_date)
    if loan_id is None:
        return None
    if not approve_loan(client, loan_id, principal, business_date):
        return None
    if not disburse_loan(client, loan_id, principal, business_date):
        return None
    return loan_id, principal


def installment_amount(principal: float) -> float:
    """A repayment roughly the size of one scheduled installment."""
    return round(principal / NUMBER_OF_REPAYMENTS, 2)
