"""Drives a mixed, repeatable banking workload against Fineract.

The run has two parts:

1. Onboarding - a number of new clients, each given an active savings account
   with an opening deposit, and some of them a disbursed loan.
2. Activity loop - randomly selected operations drawn from a weighted mix, so
   the resulting API and database load resembles ongoing branch activity rather
   than a single scripted sequence.

Runs are reproducible: the same --seed replays the same sequence of decisions.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from datetime import date

from .bootstrap import BankingSetup, bootstrap
from .client import FineractClient
from .operations import accounting, clients, loans, savings

# Relative frequency of each activity-loop operation. Deposits dominate, as they
# do in retail banking; heavier operations such as loan origination are rarer.
DEFAULT_MIX: dict[str, int] = {
    "savings.deposit": 28,
    "savings.withdrawal": 14,
    "loan.repayment": 14,
    "savings.read": 10,
    "client.read": 8,
    "client.list": 6,
    "accounting.journalentries.list": 6,
    "accounting.journalentry.create": 5,
    "accounting.journalentries.by_account": 4,
    "loan.originate": 5,
}


@dataclass
class WorkloadPools:
    """Entities created during this run, reused by the activity loop."""

    client_ids: list[int] = field(default_factory=list)
    savings_ids: list[int] = field(default_factory=list)
    loans: list[tuple[int, float]] = field(default_factory=list)  # (loan_id, principal)


@dataclass
class WorkloadOptions:
    new_clients: int = 5
    iterations: int = 40
    loans_per_run: int = 2
    seed: int | None = None
    delay_seconds: float = 0.0


def _payment_type(setup: BankingSetup, rng: random.Random) -> int:
    """Pick one of the provisioned payment types for a transaction."""
    return rng.choice(setup.payment_type_ids)


def onboard(
    client: FineractClient,
    setup: BankingSetup,
    pools: WorkloadPools,
    options: WorkloadOptions,
    rng: random.Random,
    business_date: date,
) -> None:
    """Create clients with an activated savings account and an opening deposit."""
    for _ in range(options.new_clients):
        client_id = clients.create_client(client, setup, rng, business_date)
        if client_id is None:
            continue
        pools.client_ids.append(client_id)

        savings_id = savings.open_savings_account(
            client, setup, client_id, business_date
        )
        if savings_id is None:
            continue
        pools.savings_ids.append(savings_id)

        # Opening deposit, so later withdrawals have funds to draw on.
        savings.deposit(
            client,
            savings_id,
            savings.random_deposit_amount(rng),
            business_date,
            _payment_type(setup, rng),
        )

    # Give a subset of the new clients a disbursed loan.
    for client_id in pools.client_ids[: options.loans_per_run]:
        originated = loans.originate_loan(client, setup, client_id, rng, business_date)
        if originated:
            pools.loans.append(originated)


def _perform(
    operation: str,
    client: FineractClient,
    setup: BankingSetup,
    pools: WorkloadPools,
    rng: random.Random,
    business_date: date,
) -> None:
    """Execute one activity-loop operation, skipping it if its pool is empty."""
    if operation == "savings.deposit" and pools.savings_ids:
        savings.deposit(
            client,
            rng.choice(pools.savings_ids),
            savings.random_deposit_amount(rng),
            business_date,
            _payment_type(setup, rng),
        )

    elif operation == "savings.withdrawal" and pools.savings_ids:
        savings_id = rng.choice(pools.savings_ids)
        # Read the real balance so the withdrawal amount is valid.
        balance = savings.read_account(client, savings_id)
        if balance:
            amount = savings.random_withdrawal_amount(rng, balance)
            if amount > 0:
                savings.withdraw(
                    client, savings_id, amount, business_date,
                    _payment_type(setup, rng),
                )

    elif operation == "loan.repayment" and pools.loans:
        loan_id, principal = rng.choice(pools.loans)
        loans.repay_loan(
            client, loan_id, loans.installment_amount(principal), business_date,
            _payment_type(setup, rng),
        )

    elif operation == "loan.originate" and pools.client_ids:
        originated = loans.originate_loan(
            client, setup, rng.choice(pools.client_ids), rng, business_date
        )
        if originated:
            pools.loans.append(originated)

    elif operation == "savings.read" and pools.savings_ids:
        savings.read_account(client, rng.choice(pools.savings_ids))

    elif operation == "client.read" and pools.client_ids:
        clients.read_client(client, rng.choice(pools.client_ids))

    elif operation == "client.list":
        clients.list_clients(client)

    elif operation == "accounting.journalentries.list":
        accounting.list_journal_entries(client)

    elif operation == "accounting.journalentry.create":
        accounting.post_manual_journal_entry(client, setup, rng, business_date)

    elif operation == "accounting.journalentries.by_account":
        accounting.run_trial_balance_read(client, setup)


def activity_loop(
    client: FineractClient,
    setup: BankingSetup,
    pools: WorkloadPools,
    options: WorkloadOptions,
    rng: random.Random,
    business_date: date,
    mix: dict[str, int] | None = None,
) -> None:
    """Run the weighted mix of operations for the configured iteration count."""
    weights = mix or DEFAULT_MIX
    population = list(weights.keys())
    cumulative = list(weights.values())

    for _ in range(options.iterations):
        operation = rng.choices(population, weights=cumulative, k=1)[0]
        _perform(operation, client, setup, pools, rng, business_date)
        if options.delay_seconds:
            time.sleep(options.delay_seconds)


def run_workload(
    client: FineractClient,
    options: WorkloadOptions,
    business_date: date | None = None,
) -> tuple[BankingSetup, WorkloadPools]:
    """Bootstrap configuration, onboard clients, then run the activity loop."""
    rng = random.Random(options.seed)
    today = business_date or date.today()

    setup = bootstrap(client)
    pools = WorkloadPools()
    onboard(client, setup, pools, options, rng, today)
    activity_loop(client, setup, pools, options, rng, today)
    return setup, pools
