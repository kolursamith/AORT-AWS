"""Recovery-controller adapters.

``CONTROLLER_MODE=mock``  -> :class:`MockController` (Track B runs stand-alone)
``CONTROLLER_MODE=http``  -> :class:`HttpController` against ``CONTROLLER_BASE_URL``
``CONTROLLER_MODE=baseline:<name>`` -> a reference policy from ``baseline_policies.py``
``CONTROLLER_MODE=aorts`` -> Track B's AORTS adaptive controller (``aorts/recovery/controller.py``)

Both are wrapped by :class:`ResilientController`: if the controller is slow,
down or returns something malformed, a documented conservative fallback is used
(fixed small budget per route) and the reason is recorded. The controller can
never bypass Track B's ledger checks, quotas or budget guard.

``HttpController`` speaks the shared team contract (``contracts/v1``, ``POST /v1/recovery/decisions``);
see docs/trackb/API_CONTRACTS.md section 6.
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
    """Outbound adapter to a recovery controller speaking the SHARED team contract.

    Contract (``docs/API_CONTRACTS.md``, ``contracts/v1``): ``GET /health`` and, per route,
    ``POST /v1/recovery/decisions`` with ``{experiment_id, route_id, estimated_capacity_rps,
    confidence, pending_transaction_ids}`` returning ``contracts.v1.RecoveryDecision``.

    Track B supplies the capacity estimate from its OWN independent switch measurements and the
    confidence from its telemetry-integrity verdict. Every response is validated against the shared
    model; an expired decision or a selection containing IDs Track B did not offer is rejected
    (ValueError -> ResilientController falls back). The budget is advisory: Track B converts
    ``budget_rps`` to checks per tick and still applies its own guard and gates.
    """

    name = "http"

    def __init__(
        self,
        base_url: str,
        timeout: float = 2.0,
        client: httpx.Client | None = None,
        *,
        tick_s: float = 1.0,
        experiment_id: str = "trackb-local",
    ):
        self.client = client or httpx.Client(base_url=base_url)
        self.timeout = timeout
        self.tick_s = tick_s
        self.experiment_id = experiment_id

    def health(self) -> dict:
        r = self.client.get("/health", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    @staticmethod
    def _capacity_and_confidence(route: dict) -> tuple[float, float]:
        from backend.aorts.twin.route_twin import measured_capacity

        cap = measured_capacity(route.get("measured")) or 0.0
        integrity = route.get("integrity") or {}
        conf = (
            float(integrity.get("confidence", 0.0))
            if integrity.get("verdict") == "TRUSTED"
            else 0.0
        )
        return round(cap, 3), round(min(1.0, max(0.0, conf)), 3)

    def decide(self, req: RecoveryDecisionRequest) -> RecoveryDecision:
        from contracts.v1.models import RecoveryDecision as ContractDecision

        by_route: dict[str, list[str]] = {}
        for c in sorted(req.candidates, key=lambda c: c.get("ambiguous_since") or ""):
            by_route.setdefault(c["route_id"], []).append(c["transaction_id"])
        budgets: dict[str, int] = {}
        order: list[str] = []
        predicted: dict[str, dict] = {}
        now = datetime.now(UTC)
        for route in req.routes:
            rid = route["route_id"]
            pending = by_route.get(rid, [])
            cap, conf = self._capacity_and_confidence(route)
            r = self.client.post(
                "/v1/recovery/decisions",
                timeout=self.timeout,
                json={
                    "experiment_id": self.experiment_id,
                    "route_id": rid,
                    "estimated_capacity_rps": cap,
                    "confidence": conf,
                    "pending_transaction_ids": pending,
                },
            )
            r.raise_for_status()
            d = ContractDecision.model_validate(r.json())  # strict shared-contract validation
            if d.route_id != rid:
                raise ValueError(f"decision for route {d.route_id}, asked for {rid}")
            if d.expires_at <= now:
                raise ValueError("controller decision already expired")
            unknown = set(d.selected_transaction_ids) - set(pending)
            if unknown:
                raise ValueError(
                    f"controller selected {len(unknown)} transaction(s) Track B did not offer"
                )
            budgets[rid] = min(
                len(d.selected_transaction_ids) or len(pending), int(d.budget_rps * self.tick_s)
            )
            order += list(d.selected_transaction_ids)
            predicted[rid] = {
                "budget_rps": d.budget_rps,
                "predicted_delay_ms": d.predicted_delay_ms,
                "predicted_new_ambiguous": d.predicted_new_ambiguous,
                "decision_reason": d.decision_reason,
                "expires_at": d.expires_at.isoformat(),
                "contract_decision_id": d.decision_id,
            }
        return RecoveryDecision(
            decision_id=f"ext-{uuid.uuid4().hex[:12]}",
            timestamp=now,
            route_budgets=budgets,
            transaction_ids=order,
            policy="external-controller (contracts/v1)",
            predicted=predicted,
        )

    def observe(self, observation: dict) -> None:
        """The shared contract defines no observation endpoint; observations stay in Track B."""
        return None


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
            settings.controller_base_url,
            settings.controller_timeout_seconds,
            tick_s=settings.dispatch_tick_seconds,
            experiment_id=settings.experiment_id,
        )
    elif settings.controller_mode.startswith("baseline:"):
        from backend.aorts.adapters.baseline_policies import make_policy

        primary = make_policy(settings.controller_mode.split(":", 1)[1], settings.random_seed)
    else:
        primary = MockController(
            settings.dispatch_tick_seconds, default_budget=settings.fallback_budget_per_route
        )
    return ResilientController(primary, settings.fallback_budget_per_route)
