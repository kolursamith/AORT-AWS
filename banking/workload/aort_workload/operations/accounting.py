"""Accounting / general-ledger operations.

Two distinct kinds of GL activity are exercised:

1. Automatic entries. The savings and loan products use cash-based accounting,
   so every deposit, withdrawal, disbursement and repayment already causes
   Fineract to post balanced journal entries. Reading them back is how this
   module observes real GL behaviour.

2. Manual entries. A back-office style balanced journal entry, which is a normal
   part of core-banking operation and exercises a different code path.
"""

from __future__ import annotations

import random
from datetime import date
from typing import Any

from ..bootstrap import BankingSetup
from ..client import FineractClient
from ..config import DATE_FORMAT, LOCALE
from .clients import fineract_date


def list_journal_entries(client: FineractClient, limit: int = 25) -> int | None:
    """Read recent journal entries. Returns the total count Fineract reports."""
    result, payload = client.get(
        "/journalentries",
        category="accounting",
        operation="accounting.journalentries.list",
        params={"offset": 0, "limit": limit},
    )
    if not result.ok or not isinstance(payload, dict):
        return None
    total = payload.get("totalFilteredRecords")
    return int(total) if total is not None else None


def list_gl_accounts(client: FineractClient) -> None:
    """Read the chart of accounts, as a reporting/back-office caller would."""
    client.get(
        "/glaccounts",
        category="accounting",
        operation="accounting.glaccounts.list",
    )


def post_manual_journal_entry(
    client: FineractClient,
    setup: BankingSetup,
    rng: random.Random,
    business_date: date,
) -> bool:
    """Post a balanced manual journal entry (debit cash, credit fee income).

    Represents back-office fee recognition. Debits and credits are equal, so
    Fineract accepts it as a valid double-entry transaction.
    """
    amount = float(rng.randrange(100, 5_000, 50))
    body: dict[str, Any] = {
        "officeId": setup.office_id,
        "transactionDate": fineract_date(business_date),
        "currencyCode": setup.currency_code,
        "debits": [{"glAccountId": setup.gl_accounts["cash"], "amount": amount}],
        "credits": [{"glAccountId": setup.gl_accounts["income_fees"], "amount": amount}],
        "comments": "AORT workload: back-office fee recognition",
        "dateFormat": DATE_FORMAT,
        "locale": LOCALE,
    }

    result, _ = client.post(
        "/journalentries",
        category="accounting",
        operation="accounting.journalentry.create",
        json_body=body,
        context={"amount": amount},
    )
    return result.ok


def run_trial_balance_read(client: FineractClient, setup: BankingSetup) -> None:
    """Read journal entries filtered to one GL account - a common ledger query."""
    client.get(
        "/journalentries",
        category="accounting",
        operation="accounting.journalentries.by_account",
        params={
            "glAccountId": setup.gl_accounts["cash"],
            "offset": 0,
            "limit": 25,
        },
    )
