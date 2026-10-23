"""Seed synthetic identities and accounts (idempotent). ``python -m backend.aorts.app.seed``.

All data is synthetic. Account ids look like ``A1-CUST-0001``; no real customer data.
"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert

from backend.aorts.app.config import get_settings
from backend.aorts.app.db import Database
from backend.aorts.banking.ledger.models import Account, BankIdentity

REMITTER_BANKS = ["A1", "A2", "A3"]
BENEFICIARY_BANKS = ["B", "C"]
CUSTOMERS_PER_BANK = 10


def seed(db: Database, opening_balance_paise: int, customers: int = CUSTOMERS_PER_BANK) -> dict:
    identities = [(b, f"Synthetic remitter bank {b}", "REMITTER") for b in REMITTER_BANKS]
    identities += [(b, f"Synthetic beneficiary bank {b}", "BENEFICIARY") for b in BENEFICIARY_BANKS]
    identities += [("SWITCH", "Synthetic payment switch", "SWITCH")]
    accounts = []
    for bank in REMITTER_BANKS:
        accounts += [
            (
                f"{bank}-CUST-{i:04d}",
                bank,
                f"Synthetic customer {bank}/{i}",
                "CUSTOMER",
                opening_balance_paise,
            )
            for i in range(1, customers + 1)
        ]
        accounts += [
            (f"{bank}-SUSPENSE", bank, f"{bank} in-transit suspense", "SUSPENSE", 0),
            (f"{bank}-SETTLEMENT", bank, f"{bank} outward settlement", "SETTLEMENT", 0),
        ]
    for bank in BENEFICIARY_BANKS:
        accounts += [
            (f"{bank}-CUST-{i:04d}", bank, f"Synthetic beneficiary {bank}/{i}", "CUSTOMER", 0)
            for i in range(1, customers + 1)
        ]
        accounts += [(f"{bank}-SETTLEMENT", bank, f"{bank} inward settlement", "SETTLEMENT", 0)]
    with db.session() as s:
        s.execute(
            insert(BankIdentity)
            .values(
                [
                    {"identity_id": i, "display_name": n, "role": r, "key_ids": "", "active": True}
                    for i, n, r in identities
                ]
            )
            .on_conflict_do_nothing(index_elements=["identity_id"])
        )
        s.execute(
            insert(Account)
            .values(
                [
                    {
                        "account_id": a,
                        "bank_id": b,
                        "holder_name": h,
                        "account_type": t,
                        "balance_paise": bal,
                        "opening_balance_paise": bal,
                        "version": 1,
                    }
                    for a, b, h, t, bal in accounts
                ]
            )
            .on_conflict_do_nothing(index_elements=["account_id"])
        )
    return {"identities": len(identities), "accounts": len(accounts)}


def main() -> None:
    s = get_settings()
    db = Database(s.database_url.get_secret_value(), 2)
    print(seed(db, s.accounts_seed_balance_paise))
    db.dispose()


if __name__ == "__main__":
    main()
