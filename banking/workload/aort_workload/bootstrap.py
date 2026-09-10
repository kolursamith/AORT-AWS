"""One-time (idempotent) provisioning of the AORT banking configuration.

Creates the chart of accounts, a savings product and a loan product inside a
running Fineract instance. Products are configured with CASH-BASED accounting so
that ordinary banking transactions post real double-entry journal entries -
the general-ledger behaviour is produced by Fineract itself, not simulated.

Safe to re-run: existing entities are looked up by name/code and reused.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .client import FineractClient
from .config import CURRENCY_CODE, DATE_FORMAT, LOCALE

# Fineract GL account type enum
ASSET, LIABILITY, EQUITY, INCOME, EXPENSE = 1, 2, 3, 4, 5
DETAIL_USAGE = 1

# Chart of accounts: (key, name, glCode, type)
GL_ACCOUNTS: list[tuple[str, str, str, int]] = [
    ("cash",                "AORT Cash",                  "AORT-1001", ASSET),
    ("loan_portfolio",      "AORT Loan Portfolio",        "AORT-1002", ASSET),
    ("interest_receivable", "AORT Interest Receivable",   "AORT-1003", ASSET),
    ("overdraft_portfolio", "AORT Overdraft Portfolio",   "AORT-1004", ASSET),
    # Loan products require an ASSET transfers-in-suspense account, whereas
    # savings products require a LIABILITY one, so both types are provisioned.
    ("loan_transfers_susp", "AORT Loan Transfers Susp.",  "AORT-1005", ASSET),
    ("savings_control",     "AORT Savings Control",       "AORT-2001", LIABILITY),
    ("transfers_suspense",  "AORT Transfers In Suspense", "AORT-2002", LIABILITY),
    ("overpayment",         "AORT Overpayment Liability", "AORT-2003", LIABILITY),
    ("income_interest",     "AORT Income From Interest",  "AORT-4001", INCOME),
    ("income_fees",         "AORT Income From Fees",      "AORT-4002", INCOME),
    ("income_penalties",    "AORT Income From Penalties", "AORT-4003", INCOME),
    ("income_recovery",     "AORT Income From Recovery",  "AORT-4004", INCOME),
    ("interest_expense",    "AORT Interest On Savings",   "AORT-5001", EXPENSE),
    ("write_off",           "AORT Losses Written Off",    "AORT-5002", EXPENSE),
]

SAVINGS_PRODUCT_NAME = "AORT Regular Savings"
LOAN_PRODUCT_NAME = "AORT Personal Loan"

# Payment types are Fineract's own labels for how a transaction was tendered.
# They are NOT integrations with UPI, card networks, RTGS or SWIFT - no such
# external payment ecosystem is implemented or simulated by this project.
# (name, description, isCashPayment)
PAYMENT_TYPES: list[tuple[str, str, bool]] = [
    ("AORT Cash", "Over-the-counter cash", True),
    ("AORT Internal Transfer", "Transfer between accounts within this instance", False),
    ("AORT Cheque", "Cheque presented at branch", False),
]


@dataclass
class BankingSetup:
    """Identifiers the workload operations need, resolved from live Fineract."""

    office_id: int
    savings_product_id: int
    loan_product_id: int
    gl_accounts: dict[str, int] = field(default_factory=dict)
    payment_type_ids: list[int] = field(default_factory=list)
    # Whichever of transactionProcessingStrategyCode/Id this Fineract build accepts.
    loan_strategy: dict[str, Any] = field(default_factory=dict)
    currency_code: str = CURRENCY_CODE


def _enable_currency(client: FineractClient) -> None:
    """Ensure the working currency is enabled for the tenant."""
    _, current = client.get(
        "/currencies",
        category="bootstrap",
        operation="bootstrap.currencies.read",
        required=True,
    )
    selected = [c["code"] for c in (current or {}).get("selectedCurrencyOptions", [])]
    if CURRENCY_CODE in selected:
        return

    client.put(
        "/currencies",
        category="bootstrap",
        operation="bootstrap.currencies.enable",
        json_body={"currencies": sorted(set(selected + [CURRENCY_CODE]))},
        context={"currency": CURRENCY_CODE},
        required=True,
    )


def _resolve_head_office(client: FineractClient) -> int:
    _, offices = client.get(
        "/offices",
        category="bootstrap",
        operation="bootstrap.offices.read",
        required=True,
    )
    if not offices:
        raise RuntimeError("Fineract returned no offices; tenant is not initialised")
    # The seeded head office has the lowest id.
    return min(int(o["id"]) for o in offices)


def _ensure_gl_accounts(client: FineractClient) -> dict[str, int]:
    """Create the AORT chart of accounts, reusing any that already exist."""
    _, existing = client.get(
        "/glaccounts",
        category="bootstrap",
        operation="bootstrap.glaccounts.read",
        required=True,
    )
    by_code = {a["glCode"]: int(a["id"]) for a in (existing or [])}

    accounts: dict[str, int] = {}
    for key, name, gl_code, acct_type in GL_ACCOUNTS:
        if gl_code in by_code:
            accounts[key] = by_code[gl_code]
            continue
        _, created = client.post(
            "/glaccounts",
            category="bootstrap",
            operation="bootstrap.glaccount.create",
            json_body={
                "name": name,
                "glCode": gl_code,
                "manualEntriesAllowed": True,
                "type": acct_type,
                "usage": DETAIL_USAGE,
                "description": f"AORT prototype account: {name}",
            },
            context={"glCode": gl_code},
            required=True,
        )
        accounts[key] = int(created["resourceId"])
    return accounts


def _ensure_payment_types(client: FineractClient) -> list[int]:
    """Provision the payment types savings transactions must reference."""
    _, existing = client.get(
        "/paymenttypes",
        category="bootstrap",
        operation="bootstrap.paymenttypes.read",
        required=True,
    )
    by_name = {p["name"]: int(p["id"]) for p in (existing or [])}

    ids: list[int] = []
    for position, (name, description, is_cash) in enumerate(PAYMENT_TYPES, start=1):
        if name in by_name:
            ids.append(by_name[name])
            continue
        _, created = client.post(
            "/paymenttypes",
            category="bootstrap",
            operation="bootstrap.paymenttype.create",
            json_body={
                "name": name,
                "description": description,
                "isCashPayment": is_cash,
                "position": position,
            },
            context={"paymentType": name},
            required=True,
        )
        ids.append(int(created["resourceId"]))
    return ids


def _find_product(products: Any, name: str) -> int | None:
    for product in products or []:
        if product.get("name") == name:
            return int(product["id"])
    return None


def _ensure_savings_product(client: FineractClient, gl: dict[str, int]) -> int:
    _, products = client.get(
        "/savingsproducts",
        category="bootstrap",
        operation="bootstrap.savingsproducts.read",
        required=True,
    )
    existing = _find_product(products, SAVINGS_PRODUCT_NAME)
    if existing:
        return existing

    _, created = client.post(
        "/savingsproducts",
        category="bootstrap",
        operation="bootstrap.savingsproduct.create",
        json_body={
            "name": SAVINGS_PRODUCT_NAME,
            "shortName": "ASAV",
            "description": "AORT prototype regular savings product",
            "currencyCode": CURRENCY_CODE,
            "digitsAfterDecimal": 2,
            "inMultiplesOf": 1,
            "nominalAnnualInterestRate": 4,
            "interestCompoundingPeriodType": 1,   # daily
            "interestPostingPeriodType": 4,       # monthly
            "interestCalculationType": 1,         # daily balance
            "interestCalculationDaysInYearType": 365,
            "withdrawalFeeForTransfers": False,
            "enforceMinRequiredBalance": False,
            "allowOverdraft": False,
            "accountingRule": 2,                  # CASH based -> real GL entries
            "savingsReferenceAccountId": gl["cash"],
            "savingsControlAccountId": gl["savings_control"],
            "transfersInSuspenseAccountId": gl["transfers_suspense"],
            "interestOnSavingsAccountId": gl["interest_expense"],
            "incomeFromFeeAccountId": gl["income_fees"],
            "incomeFromPenaltyAccountId": gl["income_penalties"],
            # Required by Fineract for cash-based savings accounting even when
            # overdrafts are disabled on the product.
            "overdraftPortfolioControlId": gl["overdraft_portfolio"],
            "incomeFromInterestId": gl["income_interest"],
            "writeOffAccountId": gl["write_off"],
            "locale": LOCALE,
        },
        required=True,
    )
    return int(created["resourceId"])


def _repayment_strategy(client: FineractClient) -> dict[str, Any]:
    """Read the loan template so the strategy field matches this Fineract build.

    Fineract moved from a numeric strategy id to a string code across versions,
    so the accepted field is discovered rather than assumed.
    """
    _, template = client.get(
        "/loanproducts/template",
        category="bootstrap",
        operation="bootstrap.loanproduct.template",
        required=True,
    )
    options = (template or {}).get("transactionProcessingStrategyOptions") or []
    if not options:
        return {}

    preferred = next(
        (o for o in options if "penalties" in str(o.get("name", "")).lower()),
        options[0],
    )
    if preferred.get("code"):
        return {"transactionProcessingStrategyCode": preferred["code"]}
    return {"transactionProcessingStrategyId": preferred["id"]}


def _ensure_loan_product(
    client: FineractClient, gl: dict[str, int], strategy: dict[str, Any]
) -> int:
    _, products = client.get(
        "/loanproducts",
        category="bootstrap",
        operation="bootstrap.loanproducts.read",
        required=True,
    )
    existing = _find_product(products, LOAN_PRODUCT_NAME)
    if existing:
        return existing

    body: dict[str, Any] = {
        "name": LOAN_PRODUCT_NAME,
        "shortName": "APL1",
        "description": "AORT prototype personal loan product",
        "currencyCode": CURRENCY_CODE,
        "digitsAfterDecimal": 2,
        "inMultiplesOf": 1,
        "principal": 50000,
        "minPrincipal": 5000,
        "maxPrincipal": 500000,
        "numberOfRepayments": 12,
        "repaymentEvery": 1,
        "repaymentFrequencyType": 2,          # months
        "interestRatePerPeriod": 12,
        "interestRateFrequencyType": 3,       # per year
        "amortizationType": 1,                # equal installments
        "interestType": 0,                    # declining balance
        "interestCalculationPeriodType": 1,   # same as repayment period
        "daysInYearType": 1,
        "daysInMonthType": 1,
        # Interest recalculation is left off: it schedules extra background jobs
        # that would add noise to the Phase 3 telemetry baseline.
        "isInterestRecalculationEnabled": False,
        "accountingRule": 2,                  # CASH based -> real GL entries
        "fundSourceAccountId": gl["cash"],
        "loanPortfolioAccountId": gl["loan_portfolio"],
        "transfersInSuspenseAccountId": gl["loan_transfers_susp"],
        "interestOnLoanAccountId": gl["income_interest"],
        "incomeFromFeeAccountId": gl["income_fees"],
        "incomeFromPenaltyAccountId": gl["income_penalties"],
        "incomeFromRecoveryAccountId": gl["income_recovery"],
        "writeOffAccountId": gl["write_off"],
        "overpaymentLiabilityAccountId": gl["overpayment"],
        "locale": LOCALE,
        "dateFormat": DATE_FORMAT,
    }
    body.update(strategy)

    _, created = client.post(
        "/loanproducts",
        category="bootstrap",
        operation="bootstrap.loanproduct.create",
        json_body=body,
        required=True,
    )
    return int(created["resourceId"])


def bootstrap(client: FineractClient) -> BankingSetup:
    """Provision everything the workload needs and return the resolved ids."""
    _enable_currency(client)
    office_id = _resolve_head_office(client)
    gl_accounts = _ensure_gl_accounts(client)
    payment_type_ids = _ensure_payment_types(client)
    savings_product_id = _ensure_savings_product(client, gl_accounts)
    # Resolved every run (not only on creation) because loan submissions need it too.
    strategy = _repayment_strategy(client)
    loan_product_id = _ensure_loan_product(client, gl_accounts, strategy)

    return BankingSetup(
        office_id=office_id,
        savings_product_id=savings_product_id,
        loan_product_id=loan_product_id,
        gl_accounts=gl_accounts,
        payment_type_ids=payment_type_ids,
        loan_strategy=strategy,
    )
