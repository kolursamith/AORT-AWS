"""Savings / deposit account operations.

Covers the full account lifecycle Fineract enforces:
    submit -> approve -> activate -> deposit / withdraw

Deposits and withdrawals on a cash-accounting product cause Fineract to post
double-entry journal entries of its own, which is what makes the general-ledger
activity in this workload genuine rather than staged.
"""

from __future__ import annotations

import random
from datetime import date

from ..bootstrap import BankingSetup
from ..client import FineractClient
from ..config import DATE_FORMAT, LOCALE
from .clients import fineract_date


def open_savings_account(
    client: FineractClient,
    setup: BankingSetup,
    client_id: int,
    business_date: date,
) -> int | None:
    """Submit, approve and activate a savings account. Returns the account id."""
    on_date = fineract_date(business_date)

    result, payload = client.post(
        "/savingsaccounts",
        category="savings",
        operation="savings.submit",
        json_body={
            "clientId": client_id,
            "productId": setup.savings_product_id,
            "submittedOnDate": on_date,
            "dateFormat": DATE_FORMAT,
            "locale": LOCALE,
        },
        context={"clientId": client_id},
    )
    if not result.ok:
        return None
    savings_id = int(payload["savingsId"]) if "savingsId" in payload else result.resource_id

    approve, _ = client.post(
        f"/savingsaccounts/{savings_id}",
        category="savings",
        operation="savings.approve",
        params={"command": "approve"},
        json_body={
            "approvedOnDate": on_date,
            "dateFormat": DATE_FORMAT,
            "locale": LOCALE,
        },
        context={"savingsId": savings_id},
    )
    if not approve.ok:
        return None

    activate, _ = client.post(
        f"/savingsaccounts/{savings_id}",
        category="savings",
        operation="savings.activate",
        params={"command": "activate"},
        json_body={
            "activatedOnDate": on_date,
            "dateFormat": DATE_FORMAT,
            "locale": LOCALE,
        },
        context={"savingsId": savings_id},
    )
    if not activate.ok:
        return None
    return savings_id


def deposit(
    client: FineractClient,
    savings_id: int,
    amount: float,
    business_date: date,
    payment_type_id: int,
) -> bool:
    """Post a deposit transaction."""
    result, _ = client.post(
        f"/savingsaccounts/{savings_id}/transactions",
        category="transaction",
        operation="savings.deposit",
        params={"command": "deposit"},
        json_body={
            "transactionDate": fineract_date(business_date),
            "transactionAmount": amount,
            "paymentTypeId": payment_type_id,
            "dateFormat": DATE_FORMAT,
            "locale": LOCALE,
        },
        context={
            "savingsId": savings_id,
            "amount": amount,
            "paymentTypeId": payment_type_id,
        },
    )
    return result.ok


def withdraw(
    client: FineractClient,
    savings_id: int,
    amount: float,
    business_date: date,
    payment_type_id: int,
) -> bool:
    """Post a withdrawal transaction."""
    result, _ = client.post(
        f"/savingsaccounts/{savings_id}/transactions",
        category="transaction",
        operation="savings.withdrawal",
        params={"command": "withdrawal"},
        json_body={
            "transactionDate": fineract_date(business_date),
            "transactionAmount": amount,
            "paymentTypeId": payment_type_id,
            "dateFormat": DATE_FORMAT,
            "locale": LOCALE,
        },
        context={
            "savingsId": savings_id,
            "amount": amount,
            "paymentTypeId": payment_type_id,
        },
    )
    return result.ok


def read_account(client: FineractClient, savings_id: int) -> float | None:
    """Read a savings account and return its available balance, if reported."""
    result, payload = client.get(
        f"/savingsaccounts/{savings_id}",
        category="savings",
        operation="savings.read",
        context={"savingsId": savings_id},
    )
    if not result.ok or not isinstance(payload, dict):
        return None
    summary = payload.get("summary") or {}
    balance = summary.get("availableBalance", summary.get("accountBalance"))
    return float(balance) if balance is not None else None


def random_deposit_amount(rng: random.Random) -> float:
    """A plausible retail deposit, rounded to whole currency units."""
    return float(rng.randrange(500, 25_000, 100))


def random_withdrawal_amount(rng: random.Random, balance: float) -> float:
    """A withdrawal that stays within the available balance."""
    ceiling = min(balance, 10_000.0)
    if ceiling < 100:
        return 0.0
    return float(rng.randrange(100, int(ceiling) + 1, 100))
