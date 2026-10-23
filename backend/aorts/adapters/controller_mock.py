"""Mock recovery controller so Track B runs without Track A.

This is deliberately simple and is NOT Track A's twin/optimizer. It converts the
twin's *believed* route throughput into a per-tick status-check budget and picks
the oldest ambiguous transactions first. Because it believes whatever telemetry
the twin holds, it is a good stand-in for "a controller that can be misled",
which is exactly what Security Module 2 must protect against.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from backend.aorts.contracts.requests import RecoveryDecisionRequest
from backend.aorts.contracts.responses import RecoveryDecision


class MockController:
    name = "mock"

    def __init__(
        self, tick_seconds: float = 1.0, spare_fraction: float = 0.5, default_budget: int = 2
    ):
        self.tick = tick_seconds
        self.spare = spare_fraction
        self.default_budget = default_budget
        self.observations: list[dict] = []

    def health(self) -> dict:
        return {"status": "ok", "mode": "mock", "policy": "mock-capacity-proportional"}

    def decide(self, req: RecoveryDecisionRequest) -> RecoveryDecision:
        budgets: dict[str, int] = {}
        predicted: dict[str, dict] = {}
        for route in req.routes:
            rid = route["route_id"]
            believed = route.get("twin") or {}
            tps = believed.get("throughput_tps")
            if tps is None:
                tps = (route.get("measured") or {}).get("throughput_tps")
            budget = (
                self.default_budget if tps is None else max(1, int(tps * self.tick * self.spare))
            )
            budgets[rid] = budget
            predicted[rid] = {
                "capacity_tps": tps,
                "delay_ms": believed.get("latency_p95_ms"),
                "backlog": route.get("backlog"),
            }
        by_route: dict[str, list[dict]] = {}
        for c in sorted(req.candidates, key=lambda c: c.get("ambiguous_since") or ""):
            by_route.setdefault(c["route_id"], []).append(c)
        chosen = [
            c["transaction_id"]
            for rid, cs in by_route.items()
            for c in cs[: budgets.get(rid, self.default_budget)]
        ]
        return RecoveryDecision(
            decision_id=f"dec-{uuid.uuid4().hex[:12]}",
            timestamp=datetime.now(UTC),
            route_budgets=budgets,
            transaction_ids=chosen,
            policy="mock-capacity-proportional",
            predicted=predicted,
        )

    def observe(self, observation: dict) -> None:
        self.observations.append(observation)
        del self.observations[:-1000]
