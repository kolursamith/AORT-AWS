"""Pre-dispatch safety gate: the last check before a status check is dispatched.

Applied by the dispatcher to EVERY strategy (AORTS and the baselines alike), after the BudgetGuard:

* route budget      - never more checks on a route in one tick than the guard-applied budget;
* eligibility       - the ledger's read-only eligibility check (state AMBIGUOUS, version matches,
                      allowance left, backoff elapsed);
* recovery limits   - at most ``max_inflight_per_route`` checks in flight per route;
* primary capacity  - if the switch reports the route's destination pool already at the share
                      reserved for primary payments, no new recovery check is dispatched.

Every refusal is counted by reason and returned to the caller for the decision record. The ledger's
execution-time FinancialSafetyGate still applies to anything that later moves money.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field


@dataclass
class GateResult:
    allowed: bool
    reason: str = "ok"


@dataclass
class PreDispatchGate:
    ledger: object
    max_inflight_per_route: int = 10
    primary_reserve_fraction: float = 0.25
    stats: Counter = field(default_factory=Counter)

    def check(
        self,
        txn,
        *,
        dispatched_on_route: int,
        route_budget: int,
        inflight_on_route: int,
        measured: dict | None,
    ) -> GateResult:
        if dispatched_on_route >= route_budget:
            return self._deny("route_budget_exhausted")
        if inflight_on_route + dispatched_on_route >= self.max_inflight_per_route:
            return self._deny("route_inflight_limit")
        if measured and measured.get("workers"):
            workers = int(measured["workers"])
            busy = int(measured.get("in_flight") or 0) + int(measured.get("backlog") or 0)
            reserved = (
                max(1, round(workers * self.primary_reserve_fraction))
                if self.primary_reserve_fraction
                else 0
            )
            if busy >= workers - reserved:
                return self._deny("primary_capacity_reserved")
        elig = self.ledger.eligibility(txn.transaction_id, "STATUS_CHECK", txn.version)
        if not elig.eligible:
            return self._deny(
                "ineligible:" + (elig.reasons[0].split(":")[0] if elig.reasons else "unknown")
            )
        self.stats["allowed"] += 1
        return GateResult(True)

    def _deny(self, reason: str) -> GateResult:
        self.stats[reason] += 1
        return GateResult(False, reason)
