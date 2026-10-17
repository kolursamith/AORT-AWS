"""AORTS adaptive recovery controller - Track B implementation (``AORTS_CONTROLLER_MODE=aorts``).

Per tick:
1. refresh the Route/Transaction Twins from recorded events + the latest switch measurements;
2. estimate each route's capacity with a confidence-aware lower bound (``capacity.py``);
3. evaluate a ladder of recovery budgets and pick the safest feasible one (``budget.py``);
4. rank that route's ambiguous transactions (``scheduler.py``);
5. return a ``RecoveryDecision`` whose ``predicted`` field carries every estimate, evaluated
   candidate, chosen budget, selected transaction and reason.

It is still only a RECOMMENDATION: the dispatcher's BudgetGuard and PreDispatchGate, the switch's
admission control and primary-capacity reservation, and the ledger's FinancialSafetyGate all apply
on top. Track A's controller can replace this one (``CONTROLLER_MODE=http``) without code changes.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections import defaultdict, deque
from datetime import UTC, datetime

from backend.aorts.contracts.requests import RecoveryDecisionRequest
from backend.aorts.contracts.responses import RecoveryDecision
from backend.aorts.recovery.budget import BudgetConfig, BudgetOptimizer
from backend.aorts.recovery.capacity import CapacityEstimator
from backend.aorts.recovery.scheduler import PriorityScheduler
from backend.aorts.twin.engine import TwinEngine


class AortsController:
    name = "aorts"
    policy = "aorts-adaptive"

    def __init__(
        self,
        engine: TwinEngine,
        *,
        store=None,
        budget_cfg: BudgetConfig | None = None,
        starvation_s: float = 8.0,
        selection_explain: int = 10,
    ):
        self.engine = engine
        self.store = store
        self.estimator = CapacityEstimator()
        self.optimizer = BudgetOptimizer(budget_cfg)
        self.scheduler = PriorityScheduler(starvation_s=starvation_s)
        self.explain = selection_explain
        self.p_resolve: dict[str, float] = {}
        self.pred_vs_obs: deque = deque(maxlen=500)
        self._last_pred: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.last_decision: dict | None = None

    def health(self) -> dict:
        return {
            "status": "ok",
            "mode": "aorts",
            "policy": self.policy,
            "events_processed": self.engine.events_processed,
        }

    def observe(self, observation: dict) -> None:
        """Learn the per-route probability that an admitted check yields an authoritative outcome."""
        if not observation.get("admitted"):
            return
        rid = observation.get("route_id")
        hit = 1.0 if observation.get("status") in ("SUCCESS", "FAILED") else 0.0
        with self._lock:
            prev = self.p_resolve.get(rid)
            self.p_resolve[rid] = hit if prev is None else 0.8 * prev + 0.2 * hit

    def decide(self, req: RecoveryDecisionRequest) -> RecoveryDecision:
        now = req.timestamp if req.timestamp.tzinfo else req.timestamp.replace(tzinfo=UTC)
        self.engine.poll()
        self.engine.observe_measurements(
            {
                r["route_id"].split("-", 1)[1]: r["measured"]
                for r in req.routes
                if r.get("measured")
            },
            now,
        )
        by_route = defaultdict(list)
        for c in req.candidates:
            by_route[c["route_id"]].append(c)
        budgets, order, routes_out, selection = {}, [], {}, []
        for route in req.routes:
            rid = route["route_id"]
            rt = self.engine.routes.get(rid)
            self._record_observed(rid, rt, now)
            est = self.estimator.estimate(rt)
            ranked = self.scheduler.rank(by_route.get(rid, []), self.engine.txns.twins, now)
            ttd = min((r.components["seconds_left"] for r in ranked), default=None)
            with self._lock:
                p_res = self.p_resolve.get(rid)
            dec = self.optimizer.decide(
                est,
                primary_tps=rt.primary_load_tps(now),
                backlog=max(route.get("backlog") or 0, len(ranked)),
                p50_ms=(rt.measured or {}).get("latency_p50_ms"),
                p_resolve=p_res,
                min_time_to_deadline_s=ttd,
            )
            budgets[rid] = dec.budget
            chosen = ranked[: dec.budget]
            order += [r.transaction_id for r in ranked]
            selection += [
                {
                    **r.as_dict(),
                    "selected": i < dec.budget,
                    "reason": "starvation guard (forced)" if r.forced else "priority score",
                }
                for i, r in enumerate(ranked[: max(self.explain, dec.budget)])
            ]
            routes_out[rid] = {
                "estimate": est.as_dict(),
                "decision": dec.as_dict(),
                "twin": rt.as_dict(now),
                "selected": [r.transaction_id for r in chosen],
            }
            with self._lock:
                self._last_pred[rid] = {"at": now, **dec.predicted, "budget": dec.budget}
        decision = RecoveryDecision(
            decision_id=f"aorts-{uuid.uuid4().hex[:10]}",
            timestamp=now,
            route_budgets=budgets,
            transaction_ids=order,
            policy=self.policy,
            predicted={
                "routes": routes_out,
                "selection": selection,
                "twin_event_stats": dict(self.engine.txns.stats),
            },
        )
        self.last_decision = decision.model_dump(mode="json")
        self._publish(now)
        return decision

    def _record_observed(self, rid: str, rt, now: datetime) -> None:
        """Predicted-vs-observed: compare last tick's predicted primary latency with what was measured now."""
        with self._lock:
            prev = self._last_pred.get(rid)
        observed = (rt.measured or {}).get("latency_p50_ms")
        if prev and observed is not None and prev.get("pred_primary_latency_ms") is not None:
            self.pred_vs_obs.append(
                {
                    "route_id": rid,
                    "at": now.isoformat(),
                    "budget": prev["budget"],
                    "pred_primary_latency_ms": prev["pred_primary_latency_ms"],
                    "observed_latency_p50_ms": observed,
                    "pred_capacity_low_rps": prev.get("capacity_low_rps"),
                }
            )

    def _publish(self, now: datetime) -> None:
        if self.store is None:
            return
        from backend.aorts.adapters.twin_state import update_with_retry

        snap = self.engine.snapshot(now)
        snap["last_decision"] = self.last_decision
        snap["predicted_vs_observed"] = list(self.pred_vs_obs)[-100:]
        try:
            update_with_retry(self.store, "aorts:snapshot", lambda _: snap)
        except Exception:  # noqa: BLE001 - publishing for the dashboard must never break recovery
            logging.getLogger(__name__).debug("best-effort step failed", exc_info=True)
