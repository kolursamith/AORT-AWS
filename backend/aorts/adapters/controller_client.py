"""Track A controller adapters.

``CONTROLLER_MODE=mock``  -> :class:`MockController` (Track B runs stand-alone)
``CONTROLLER_MODE=http``  -> :class:`HttpController` against ``CONTROLLER_BASE_URL``
``CONTROLLER_MODE=baseline:<name>`` -> a reference policy from ``baseline_policies.py``
``CONTROLLER_MODE=aorts`` -> Track B's AORTS adaptive controller (``aorts/recovery/controller.py``)

Both are wrapped by :class:`ResilientController`: if the controller is slow,
down or returns something malformed, a documented conservative fallback is used
(fixed small budget per route) and the reason is recorded. The controller can
never bypass Track B's ledger checks, quotas or budget guard.

The HTTP contract assumed here is documented in API_CONTRACTS.md section 6 and is
a PROPOSAL until Track A confirms its real endpoints.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Protocol

import httpx
from pydantic import ValidationError

from backend.aorts.adapters.controller_mock import MockController
from backend.aorts.app.logging_config import log_event
from backend.aorts.contracts.requests import RecoveryDecisionRequest
from backend.aorts.contracts.responses import RecoveryDecision

logger = logging.getLogger("aorts.controller")


class Controller(Protocol):
    name: str

    def health(self) -> dict: ...

    def decide(self, req: RecoveryDecisionRequest) -> RecoveryDecision: ...

    def observe(self, observation: dict) -> None: ...


class HttpController:
    name = "http"

    def __init__(self, base_url: str, timeout: float = 2.0, client: httpx.Client | None = None):
        self.client = client or httpx.Client(base_url=base_url)
        self.timeout = timeout

    def health(self) -> dict:
        r = self.client.get("/api/v1/controller/health", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def decide(self, req: RecoveryDecisionRequest) -> RecoveryDecision:
        r = self.client.post(
            "/api/v1/controller/decisions", json=req.model_dump(mode="json"), timeout=self.timeout
        )
        r.raise_for_status()
        return RecoveryDecision.model_validate(r.json())

    def observe(self, observation: dict) -> None:
        r = self.client.post(
            "/api/v1/controller/observations", json=observation, timeout=self.timeout
        )
        r.raise_for_status()


class ResilientController:
    def __init__(self, primary: Controller, fallback_budget: int):
        self.primary = primary
        self.fallback_budget = fallback_budget
        self.name = primary.name
        self.last_error: str | None = None

    def health(self) -> dict:
        try:
            return {"reachable": True, "mode": self.primary.name, **self.primary.health()}
        except Exception as exc:  # noqa: BLE001
            return {"reachable": False, "mode": self.primary.name, "error": type(exc).__name__}

    def fallback(self, req: RecoveryDecisionRequest, reason: str) -> RecoveryDecision:
        return RecoveryDecision(
            decision_id=f"fb-{uuid.uuid4().hex[:12]}",
            timestamp=datetime.now(UTC),
            route_budgets={r["route_id"]: self.fallback_budget for r in req.routes},
            transaction_ids=[],
            policy="fallback-fixed-budget",
            fallback=True,
            fallback_reason=reason,
        )

    def decide(self, req: RecoveryDecisionRequest) -> RecoveryDecision:
        try:
            decision = self.primary.decide(req)
            self.last_error = None
            return decision
        except (
            httpx.HTTPError,
            ValidationError,
            ValueError,
            KeyError,
            TypeError,
            ArithmeticError,
        ) as exc:
            self.last_error = f"{type(exc).__name__}: {str(exc)[:120]}"
            log_event(
                logger,
                "controller unavailable; using conservative fallback",
                logging.WARNING,
                error=self.last_error,
            )
            return self.fallback(req, f"controller error: {type(exc).__name__}")

    def observe(self, observation: dict) -> None:
        try:
            self.primary.observe(observation)
        except Exception as exc:  # noqa: BLE001 - observations are best-effort
            log_event(
                logger,
                "controller observation not delivered",
                logging.DEBUG,
                error=type(exc).__name__,
            )


def make_controller(settings, *, db=None, store=None) -> ResilientController:
    if settings.controller_mode == "aorts":
        from backend.aorts.recovery.budget import BudgetConfig
        from backend.aorts.recovery.controller import AortsController
        from backend.aorts.twin.engine import TwinEngine

        primary: Controller = AortsController(
            TwinEngine(db),
            store=store,
            budget_cfg=BudgetConfig(
                tick_s=settings.dispatch_tick_seconds,
                fallback_budget=settings.fallback_budget_per_route,
                hard_cap=settings.max_budget_per_route,
            ),
        )
        return ResilientController(primary, settings.fallback_budget_per_route)
    if settings.controller_mode == "http":
        primary: Controller = HttpController(
            settings.controller_base_url, settings.controller_timeout_seconds
        )
    elif settings.controller_mode.startswith("baseline:"):
        from backend.aorts.adapters.baseline_policies import make_policy

        primary = make_policy(settings.controller_mode.split(":", 1)[1], settings.random_seed)
    else:
        primary = MockController(
            settings.dispatch_tick_seconds, default_budget=settings.fallback_budget_per_route
        )
    return ResilientController(primary, settings.fallback_budget_per_route)
