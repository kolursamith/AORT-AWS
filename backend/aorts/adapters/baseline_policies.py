"""Baseline recovery policies for comparison runs (Weakness E: "weak baseline").

These are deliberately simple REFERENCE policies, NOT the AORTS controller (Track A's
twin/look-ahead optimizer plugs in through ``CONTROLLER_MODE=http``). Each implements the
same ``Controller`` interface, so the identical workload can be replayed against every
policy and the exported metrics compared.

``npci-static`` is a *simplified, simulation-only* schedule inspired by fixed status-check
intervals. It is NOT a statement of any current NPCI/RBI rule.

Times are simulated seconds since the transaction became ambiguous.
"""

from __future__ import annotations

import random
import uuid
from datetime import UTC, datetime

from backend.aorts.contracts.requests import RecoveryDecisionRequest
from backend.aorts.contracts.responses import RecoveryDecision


def _age_s(candidate: dict, now: datetime) -> float:
    since = candidate.get("ambiguous_since")
    if not since:
        return 0.0
    return (now - datetime.fromisoformat(since)).total_seconds()


class _Base:
    name = "base"
    policy = "base"

    def __init__(self):
        self.observations: list[dict] = []

    def health(self) -> dict:
        return {"status": "ok", "mode": "baseline", "policy": self.policy}

    def observe(self, observation: dict) -> None:
        self.observations.append(observation)
        del self.observations[:-2000]

    def _decision(self, budgets: dict[str, int], txn_ids: list[str]) -> RecoveryDecision:
        return RecoveryDecision(
            decision_id=f"{self.policy}-{uuid.uuid4().hex[:10]}",
            timestamp=datetime.now(UTC),
            route_budgets=budgets,
            transaction_ids=txn_ids,
            policy=self.policy,
        )


class NaivePolling(_Base):
    """Check every eligible ambiguous transaction on every tick."""

    name = policy = "naive-polling"

    def decide(self, req: RecoveryDecisionRequest) -> RecoveryDecision:
        budgets: dict[str, int] = {}
        for c in req.candidates:
            budgets[c["route_id"]] = budgets.get(c["route_id"], 0) + 1
        return self._decision(budgets, [c["transaction_id"] for c in req.candidates])


class StaticSchedule(_Base):
    """Fixed check times after ambiguity (simulation-only, 'NPCI-inspired' style schedule)."""

    name = policy = "npci-static"

    def __init__(self, schedule_s: tuple[float, ...] = (1.0, 3.0, 6.0, 10.0, 15.0, 21.0)):
        super().__init__()
        self.schedule = schedule_s

    def decide(self, req: RecoveryDecisionRequest) -> RecoveryDecision:
        now = req.timestamp
        chosen, budgets = [], {}
        for c in req.candidates:
            k = c.get("checks_used", 0)
            if k < len(self.schedule) and _age_s(c, now) >= self.schedule[k]:
                chosen.append(c["transaction_id"])
                budgets[c["route_id"]] = budgets.get(c["route_id"], 0) + 1
        return self._decision(budgets, chosen)


class ExponentialBackoffJitter(_Base):
    """Per transaction: next check after base * 2^k seconds, with full jitter."""

    name = policy = "exp-backoff-jitter"

    def __init__(self, base_s: float = 0.5, cap_s: float = 20.0, seed: int = 42):
        super().__init__()
        self.base, self.cap = base_s, cap_s
        self.rng = random.Random(seed)
        self.next_at: dict[str, float] = {}

    def decide(self, req: RecoveryDecisionRequest) -> RecoveryDecision:
        now = req.timestamp
        chosen, budgets = [], {}
        for c in req.candidates:
            tid, age = c["transaction_id"], _age_s(c, now)
            due = self.next_at.get(tid)
            if due is None or age >= due:
                k = c.get("checks_used", 0)
                self.next_at[tid] = age + self.rng.uniform(0, min(self.cap, self.base * 2**k))
                chosen.append(tid)
                budgets[c["route_id"]] = budgets.get(c["route_id"], 0) + 1
        return self._decision(budgets, chosen)


class FixedBudget(_Base):
    """A constant number of checks per route per tick, oldest first."""

    name = policy = "fixed-budget"

    def __init__(self, per_route: int = 3):
        super().__init__()
        self.per_route = per_route

    def decide(self, req: RecoveryDecisionRequest) -> RecoveryDecision:
        budgets = {r["route_id"]: self.per_route for r in req.routes}
        ordered = sorted(req.candidates, key=lambda c: c.get("ambiguous_since") or "")
        return self._decision(budgets, [c["transaction_id"] for c in ordered])


class Aimd(_Base):
    """Additive-increase / multiplicative-decrease budget per route, driven by observed check outcomes.

    +1 per tick while the last observations are healthy (admitted, resolved, latency under target);
    halve on any rejection, unresolved check or slow check.
    """

    name = policy = "aimd"

    def __init__(
        self, start: int = 2, floor: int = 1, ceiling: int = 50, latency_target_ms: float = 500.0
    ):
        super().__init__()
        self.budget: dict[str, float] = {}
        self.start, self.floor, self.ceiling, self.target = start, floor, ceiling, latency_target_ms
        self._seen = 0

    def decide(self, req: RecoveryDecisionRequest) -> RecoveryDecision:
        fresh = self.observations[self._seen :]
        self._seen = len(self.observations)
        by_route: dict[str, list[dict]] = {}
        for o in fresh:
            by_route.setdefault(o["route_id"], []).append(o)
        budgets = {}
        for r in req.routes:
            rid = r["route_id"]
            b = self.budget.get(rid, float(self.start))
            obs = by_route.get(rid, [])
            bad = any(
                (not o.get("admitted"))
                or o.get("status") not in ("SUCCESS", "FAILED")
                or (o.get("latency_ms") or 0) > self.target
                for o in obs
            )
            b = max(self.floor, b / 2) if bad else min(self.ceiling, b + 1)
            self.budget[rid] = b
            budgets[rid] = int(b)
        ordered = sorted(req.candidates, key=lambda c: c.get("ambiguous_since") or "")
        return self._decision(budgets, [c["transaction_id"] for c in ordered])


POLICIES = {
    "naive-polling": NaivePolling,
    "npci-static": StaticSchedule,
    "exp-backoff-jitter": ExponentialBackoffJitter,
    "fixed-budget": FixedBudget,
    "aimd": Aimd,
}


def make_policy(name: str, seed: int = 42):
    from backend.aorts.adapters.controller_mock import MockController

    if name == "mock":
        return MockController()
    cls = POLICIES[name]
    return cls(seed=seed) if cls is ExponentialBackoffJitter else cls()
