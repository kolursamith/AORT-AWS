"""Recovery-budget optimizer (AORTS, Track B implementation).

For each route it evaluates a ladder of candidate budgets (status checks per tick) with a simple,
explainable queueing prediction and picks the SAFEST FEASIBLE one:

1. Reserve primary-payment capacity: ``reserved = primary_load * (1 + primary_margin)``.
2. Usable capacity: ``rho_max * capacity_low`` (conservative lower bound, not the point estimate).
   Recovery may only use what is left after the reservation: ``reserved + B/tick <= rho_max * capacity_low``.
3. For candidate B (checks/tick): ``load = primary + B / tick``; ``rho = load / capacity_low``;
   predicted primary latency ``= p50 / (1 - rho)`` (M/M/1-style inflation, explainable, not exact).
4. Feasible iff ``primary * (1 + primary_margin) + B/tick <= rho_max * capacity_low`` AND the
   predicted primary latency stays within ``latency_slo_factor x`` the measured p50 baseline.
5. Predicted backlog clearance ``= backlog / (B / tick * p_resolve)``.
6. Choose the SMALLEST feasible B whose clearance meets the target (half of the time left to the
   most urgent deadline, capped by ``target_clearance_s``); otherwise the LARGEST feasible B
   (best effort); with no feasible B > 0, 0 - unless a deadline is imminent, then a trickle of 1
   if even that keeps rho < 1.
7. Unreliable telemetry or low confidence -> conservative fallback ``min(fallback, best feasible)``.

Every decision returns all evaluated candidates with predictions and the reasons for the choice.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from backend.aorts.recovery.capacity import CapacityEstimate

LADDER = (0, 1, 2, 3, 5, 8, 13, 21, 34, 55)


@dataclass
class BudgetConfig:
    tick_s: float = 1.0
    rho_max: float = 0.75
    primary_margin: float = 0.25
    latency_slo_factor: float = 3.0
    target_clearance_s: float = 10.0
    min_confidence: float = 0.35
    min_reliability: float = 0.5
    fallback_budget: int = 2
    hard_cap: int = 20
    default_p_resolve: float = 0.5


@dataclass
class BudgetDecision:
    route_id: str
    budget: int
    mode: str  # optimized | fallback | none
    reasons: list[str] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)
    predicted: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


class BudgetOptimizer:
    def __init__(self, cfg: BudgetConfig | None = None):
        self.cfg = cfg or BudgetConfig()

    def decide(
        self,
        est: CapacityEstimate,
        *,
        primary_tps: float,
        backlog: int,
        p50_ms: float | None,
        p_resolve: float | None = None,
        min_time_to_deadline_s: float | None = None,
    ) -> BudgetDecision:
        c = self.cfg
        reasons: list[str] = []
        if backlog <= 0:
            return BudgetDecision(est.route_id, 0, "none", ["no recovery backlog"])
        if est.low_rps is None or not p50_ms:
            return BudgetDecision(
                est.route_id,
                min(c.fallback_budget, backlog),
                "fallback",
                ["no independent capacity measurement -> conservative fallback"],
            )
        p_res = p_resolve if p_resolve is not None else c.default_p_resolve
        p_res = min(1.0, max(0.05, p_res))
        cap = est.low_rps
        reserved = primary_tps * (1 + c.primary_margin)  # primary payments keep this, always
        target = c.target_clearance_s
        if min_time_to_deadline_s is not None:
            target = max(c.tick_s, min(target, 0.5 * min_time_to_deadline_s))
        cands = []
        for b in LADDER:
            if b > c.hard_cap or (b > 0 and b > 2 * backlog and b > LADDER[1]):
                break
            load = primary_tps + b / c.tick_s
            rho = load / cap if cap > 0 else float("inf")
            lat = p50_ms / (1 - rho) if rho < 1 else float("inf")
            clear = (backlog / (b / c.tick_s * p_res)) if b > 0 else float("inf")
            feasible = (
                reserved + b / c.tick_s <= c.rho_max * cap
            ) and lat <= c.latency_slo_factor * p50_ms
            cands.append(
                {
                    "budget": b,
                    "rho": round(rho, 3),
                    "pred_primary_latency_ms": _r(lat),
                    "pred_clearance_s": _r(clear),
                    "feasible": bool(feasible),
                }
            )
        feasible = [x for x in cands if x["feasible"] and x["budget"] > 0]
        reasons.append(
            f"capacity low-bound {cap:.1f} rps (point {est.point_rps:.1f}, confidence {est.confidence:.2f})"
        )
        reasons.append(
            f"primary load {primary_tps:.2f} rps reserved with +{int(c.primary_margin * 100)}% margin; "
            f"rho_max {c.rho_max}"
        )
        if feasible:
            meeting = [
                x
                for x in feasible
                if x["pred_clearance_s"] is not None and x["pred_clearance_s"] <= target
            ]
            if meeting:
                chosen, why = (
                    meeting[0],
                    f"smallest feasible budget clearing backlog within {target:.1f}s",
                )
            else:
                chosen, why = (
                    feasible[-1],
                    f"no feasible budget meets {target:.1f}s target: largest feasible",
                )
        else:
            urgent = min_time_to_deadline_s is not None and min_time_to_deadline_s < 3 * c.tick_s
            one = next((x for x in cands if x["budget"] == 1), None)
            if urgent and one and one["rho"] < 1:
                chosen, why = one, "no feasible budget, deadline imminent: trickle of 1 (rho < 1)"
            else:
                chosen, why = cands[0], "no feasible budget > 0 without harming primary payments"
        mode = "optimized"
        budget = chosen["budget"]
        if est.confidence < c.min_confidence or est.telemetry_reliability < c.min_reliability:
            capped = (
                min(c.fallback_budget, budget)
                if budget
                else min(c.fallback_budget, 1 if feasible else 0)
            )
            reasons.append(
                f"telemetry reliability {est.telemetry_reliability:.2f} / confidence {est.confidence:.2f} "
                f"below threshold -> conservative fallback {capped}"
            )
            budget, mode = capped, "fallback"
        reasons.append(why)
        pred = next((x for x in cands if x["budget"] == budget), chosen)
        return BudgetDecision(
            est.route_id,
            budget,
            mode,
            reasons,
            cands,
            {
                "capacity_low_rps": round(cap, 3),
                "capacity_point_rps": round(est.point_rps, 3),
                "confidence": round(est.confidence, 3),
                "primary_tps": primary_tps,
                "pred_primary_latency_ms": pred["pred_primary_latency_ms"],
                "pred_clearance_s": pred["pred_clearance_s"],
                "p_resolve": round(p_res, 3),
                "target_clearance_s": round(target, 2),
                "baseline_p50_ms": p50_ms,
            },
        )


def _r(x):
    return None if x in (None, float("inf")) else round(x, 2)
