"""Tier 2 status-check credits (a mechanism proposed by THIS project, not a regulatory standard).

Policy
------
* **Balance**: each remitter bank holds a credit balance, capped at ``cap``.
* **Allocation / refill**: credits accrue continuously at ``refill_per_sec``.
  The recovery controller may re-allocate a route's recovery budget across banks
  with :meth:`allocate` (proportional to each bank's ambiguous backlog, with a
  floor so no bank is starved); without a controller the static default applies.
* **Consumption**: one credit per status check the switch *admits*. Requests
  rejected earlier (auth, global/bank/route rate limit) consume nothing; a
  credit-rejected request consumes nothing and does NOT count as a completed
  check against the transaction's allowance.
* **Exhaustion**: the switch answers ``429 CREDIT_EXHAUSTED`` with a
  ``retry_after`` hint. The caller's transaction stays AMBIGUOUS.
* **Interaction**: credits are checked after the global ceiling, the per-bank
  token bucket and the per-route token bucket; all must pass, and only then are
  all of them debited atomically (under one lock).
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class CreditAccount:
    balance: float
    cap: float
    refill_per_sec: float
    updated: float

    def refill(self, now: float) -> None:
        self.balance = min(self.cap, self.balance + (now - self.updated) * self.refill_per_sec)
        self.updated = now


class Tier2CreditLedger:
    def __init__(self, default_cap: float, default_refill: float, clock=time.monotonic):
        self.default_cap = default_cap
        self.default_refill = default_refill
        self.clock = clock
        self.accounts: dict[str, CreditAccount] = {}

    def _acct(self, bank_id: str) -> CreditAccount:
        acct = self.accounts.get(bank_id)
        if acct is None:
            acct = CreditAccount(
                self.default_cap, self.default_cap, self.default_refill, self.clock()
            )
            self.accounts[bank_id] = acct
        acct.refill(self.clock())
        return acct

    def available(self, bank_id: str) -> bool:
        return self._acct(bank_id).balance >= 1.0

    def consume(self, bank_id: str) -> None:
        self._acct(bank_id).balance -= 1.0

    def retry_after(self, bank_id: str) -> float:
        acct = self._acct(bank_id)
        if acct.balance >= 1.0 or acct.refill_per_sec <= 0:
            return 0.0
        return (1.0 - acct.balance) / acct.refill_per_sec

    def view(self, bank_id: str) -> dict:
        acct = self._acct(bank_id)
        return {
            "credits": round(acct.balance, 3),
            "credit_cap": acct.cap,
            "credit_refill_per_sec": acct.refill_per_sec,
        }

    def allocate(
        self, total_refill_per_sec: float, backlog_by_bank: dict[str, int], floor_share: float = 0.1
    ) -> dict:
        """Split a recovery budget (checks/sec) across banks by backlog, with a minimum floor."""
        banks = list(backlog_by_bank)
        if not banks:
            return {}
        floor = total_refill_per_sec * floor_share / len(banks)
        remaining = max(0.0, total_refill_per_sec - floor * len(banks))
        total_backlog = sum(max(0, b) for b in backlog_by_bank.values())
        out = {}
        for bank in banks:
            share = (backlog_by_bank[bank] / total_backlog) if total_backlog else 1 / len(banks)
            rate = floor + remaining * share
            acct = self._acct(bank)
            acct.refill_per_sec = rate
            acct.cap = max(1.0, rate * 10)
            acct.balance = min(acct.balance, acct.cap)
            out[bank] = round(rate, 3)
        return out

    def reset(self) -> None:
        self.accounts.clear()
