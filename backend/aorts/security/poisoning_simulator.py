"""Attack 2: digital-twin telemetry poisoning (test-only).

Model-in-the-loop experiment, clearly a SIMULATION:

* :class:`RouteModel` is a small fluid-queue model of one route (Bank B). It is the
  ground truth and plays the role of the switch's *independent* measurements.
* A probe reports telemetry each simulated second. From ``attack_start`` on, a
  poisoning case rewrites what the probe reports. The clean series is kept
  immutable (``clean_value`` column) and never overwritten.
* The REAL :class:`TelemetryValidator`, twin store update rule, :class:`MockController`
  and :class:`BudgetGuard` decide the status-check budget. The admitted checks are
  fed back into the route model, so a misled controller really overloads the route.

Arms: ``clean`` (no poisoning), ``poisoned_unprotected`` (validation + integrity
guard disabled), ``poisoned_protected`` (defenses enabled) and ``poisoned_protected_nobias``
(defenses enabled but the persistent-bias CUSUM check switched off, i.e. the validator as it
was before that check existed). The static hard cap applies in every arm.

Case groups: value lies (``latency_low``, ``throughput_high``, ``timeout_low``, ``timeout_high``,
``conflicting_timeout_backlog``, ``backlog_inconsistent``, ``subtle_throughput_high`` = +30 %,
``subtle_throughput_8pct`` = +8 %, below the bias allowance), freshness (``replay_old``,
``stale_timestamp``, ``missing`` = the probe goes silent), identity (``wrong_source`` = signed by
probe 2 while claiming probe 1, ``unregistered_source``) and ``invalid_values``.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from backend.aorts.adapters.controller_mock import MockController
from backend.aorts.adapters.twin_state import LocalVersionedStore
from backend.aorts.contracts.requests import RecoveryDecisionRequest, RouteTelemetry
from backend.aorts.control.telemetry_service import apply_to_twin
from backend.aorts.security.budget_guard import BudgetGuard
from backend.aorts.security.telemetry_validator import TelemetryValidator

ROUTE = "A1-B"
CASES = [
    "latency_low",
    "throughput_high",
    "conflicting_timeout_backlog",
    "replay_old",
    "invalid_values",
    "subtle_throughput_high",
]
EXTENDED_CASES = [
    *CASES,
    "timeout_low",
    "timeout_high",
    "backlog_inconsistent",
    "stale_timestamp",
    "missing",
    "wrong_source",
    "unregistered_source",
    "subtle_throughput_8pct",
]
# Which identity signs the observation (identity attacks only; everything else is probe 1).
SIGNERS = {"wrong_source": "twin-probe-2", "unregistered_source": "rogue-probe"}
ARMS = ("clean", "poisoned_unprotected", "poisoned_protected", "poisoned_protected_nobias")


@dataclass
class RouteModel:
    """Fluid queue: ``workers`` servers, base service time ``service_s``, capacity mu = workers / service_s."""

    workers: int = 8
    service_s: float = 0.2
    payment_rps: float = 12.0
    timeout_ms: float = 1000.0
    backlog: float = 0.0

    @property
    def mu(self) -> float:
        return self.workers / self.service_s

    def delay_ms(self) -> float:
        return self.service_s * 1000.0 + 1000.0 * self.backlog / self.mu

    def step(self, check_rps: float, dt: float = 1.0) -> dict:
        offered = self.payment_rps + check_rps
        served = min(self.mu, offered + self.backlog / dt)
        self.backlog = max(0.0, self.backlog + (offered - self.mu) * dt)
        d = self.delay_ms()
        in_flight = min(self.workers, offered * self.service_s)
        return {
            "latency_p50_ms": round(d, 2),
            "latency_p95_ms": round(d * 1.6, 2),
            "throughput_tps": round(served, 2),
            "timeout_rate": round(min(1.0, max(0.0, (d - self.timeout_ms) / self.timeout_ms)), 4),
            "backlog": int(self.backlog),
            "workers": self.workers,
            "in_flight": int(round(in_flight)),
            "samples": 100,
        }


@dataclass
class PoisonConfig:
    case: str = "throughput_high"
    arm: str = "poisoned_protected"  # clean | poisoned_unprotected | poisoned_protected
    ticks: int = 60
    attack_start: int = 10
    degrade_at: int = 20  # Bank B slows down: service time doubles (capacity halves)
    seed: int = 42
    hard_cap: int = 30
    fallback_budget: int = 2
    max_telemetry_age_s: float = 30.0


def _poison(
    case: str,
    clean: dict,
    tick: int,
    rng: random.Random,
    history: list[RouteTelemetry],
    base: RouteTelemetry,
) -> RouteTelemetry:
    r = dict(clean)
    if case == "latency_low":
        r["latency_p50_ms"] *= 0.2
        r["latency_p95_ms"] *= 0.2
    elif case == "throughput_high":
        r["throughput_tps"] *= 4.0
    elif case == "subtle_throughput_high":
        r["throughput_tps"] *= 1.3  # within tolerance: designed to evade anomaly checks
    elif case == "subtle_throughput_8pct":
        r["throughput_tps"] *= 1.08  # below the bias allowance: the documented detection limit
    elif case == "timeout_low":
        r["timeout_rate"] = 0.0
        r["latency_p50_ms"] *= 0.5
        r["latency_p95_ms"] *= 0.5
    elif case == "timeout_high":
        r["timeout_rate"] = 0.6  # pessimistic lie: can only lower the budget (availability harm)
    elif case == "backlog_inconsistent":
        r["backlog"] = 0 if tick % 2 else 400
    elif case == "stale_timestamp":
        return base.model_copy(
            update={
                "timestamp": base.timestamp - timedelta(seconds=120),
                "event_id": f"tel-st-{tick}",
            }
        )
    elif case == "wrong_source":
        return base.model_copy(update={"source_id": "twin-probe-1"})  # signer is probe 2
    elif case == "unregistered_source":
        return base.model_copy(update={"source_id": "rogue-probe"})
    elif case == "conflicting_timeout_backlog":
        r["timeout_rate"], r["backlog"] = 0.0, 0
        r["latency_p50_ms"] = min(r["latency_p50_ms"], 200.0)
        r["latency_p95_ms"] = min(r["latency_p95_ms"], 320.0)
        r["throughput_tps"] = max(r["throughput_tps"], 40.0)
    elif case == "replay_old":
        old = history[min(len(history) - 1, 5)]  # a healthy, previously accepted observation
        if tick % 2:
            return old  # byte-identical replay (same event id + sequence)
        return old.model_copy(
            update={"event_id": f"tel-{uuid.uuid4().hex[:12]}"}
        )  # new id, old seq/timestamp
    elif case == "invalid_values":
        variant = tick % 4
        if variant == 0:
            r["latency_p50_ms"] = -5.0
        elif variant == 1:
            r["timeout_rate"] = 3.0
        elif variant == 2:
            r = {k: None for k in r}
        else:
            r["latency_p95_ms"] = r["latency_p50_ms"] / 2
    return base.model_copy(update={k: v for k, v in r.items() if k in RouteTelemetry.model_fields})


def run_poisoning(cfg: PoisonConfig) -> dict:
    rng = random.Random(cfg.seed)
    model = RouteModel()
    validator = TelemetryValidator(
        sources={"twin-probe-1"},
        max_age_s=30,
        min_confidence=0.6,
        tolerance=0.5,
        enabled=(cfg.arm != "poisoned_unprotected"),
        bias_check=(cfg.arm != "poisoned_protected_nobias"),
    )
    guard = BudgetGuard(
        hard_cap=cfg.hard_cap,
        fallback_budget=cfg.fallback_budget,
        enabled=(cfg.arm != "poisoned_unprotected"),
        max_telemetry_age_s=cfg.max_telemetry_age_s,
    )
    twin = LocalVersionedStore()
    controller = MockController(tick_seconds=1.0, default_budget=cfg.fallback_budget)
    start = datetime.now(UTC)
    measured = model.step(0)
    rows: list[dict] = []
    history: list[RouteTelemetry] = []
    applied_budget = 0
    for tick in range(cfg.ticks):
        now = start + timedelta(seconds=tick)
        if tick == cfg.degrade_at:
            model.service_s *= 2.0
        noise = {
            k: v * (1 + rng.uniform(-0.05, 0.05)) if isinstance(v, float) else v
            for k, v in measured.items()
        }
        clean = {
            k: noise[k]
            for k in (
                "latency_p50_ms",
                "latency_p95_ms",
                "throughput_tps",
                "timeout_rate",
                "backlog",
            )
        }
        clean["timeout_rate"] = min(
            1.0, max(0.0, clean["timeout_rate"])
        )  # keep the honest probe in range
        clean["latency_p95_ms"] = max(clean["latency_p95_ms"], clean["latency_p50_ms"])
        base = RouteTelemetry(
            event_id=f"tel-{cfg.seed}-{tick}",
            source_id="twin-probe-1",
            route_id=ROUTE,
            sequence=tick + 1,
            timestamp=now,
            health_score=1.0 - clean["timeout_rate"],
            **{k: (int(v) if k == "backlog" else round(v, 3)) for k, v in clean.items()},
        )
        attacking = cfg.arm != "clean" and tick >= cfg.attack_start
        obs = _poison(cfg.case, clean, tick, rng, history, base) if attacking else base
        if not attacking:
            history.append(base)
        signer = SIGNERS.get(cfg.case, "twin-probe-1") if attacking else "twin-probe-1"
        silent = attacking and cfg.case == "missing"
        if silent:  # the probe sends nothing: no new verdict, the twin keeps its last state
            assessment = None
        else:
            assessment = validator.validate(
                obs, authenticated_source=signer, measured=measured, now=now
            )
            apply_to_twin(twin, obs, assessment)
        believed = (twin.get(f"route:{ROUTE}") or (0, {}))[1]
        integrity = (twin.get(f"integrity:{ROUTE}") or (0, {}))[1]
        decision = controller.decide(
            RecoveryDecisionRequest(
                decision_request_id=f"drq-{tick}",
                timestamp=now,
                routes=[{"route_id": ROUTE, "backlog": 50, "measured": measured, "twin": believed}],
                candidates=[],
            )
        )
        recommended = decision.route_budgets[ROUTE]
        rb = guard.apply(ROUTE, recommended, telemetry=integrity, measured=measured, now=now)
        applied_budget = rb.applied
        actual_before = model.delay_ms()
        measured = model.step(applied_budget)
        rows.append(
            {
                "tick": tick,
                "attacking": attacking,
                "case": cfg.case if attacking else "none",
                "clean_throughput_tps": round(clean["throughput_tps"], 3),
                "reported_throughput_tps": None if silent else obs.throughput_tps,
                "clean_latency_p95_ms": round(clean["latency_p95_ms"], 2),
                "reported_latency_p95_ms": None if silent else obs.latency_p95_ms,
                "clean_timeout_rate": round(clean["timeout_rate"], 4),
                "reported_timeout_rate": None if silent else obs.timeout_rate,
                "clean_backlog": clean["backlog"],
                "reported_backlog": None if silent else obs.backlog,
                "verdict": "MISSING" if silent else assessment.verdict.value,
                "confidence": None if silent else round(assessment.confidence, 3),
                "reasons": "probe silent" if silent else "; ".join(assessment.reasons)[:300],
                "guard_integrity": rb.integrity,
                "capacity_estimate_tps": believed.get("throughput_tps"),
                "budget_recommended": recommended,
                "budget_applied": applied_budget,
                "predicted_delay_ms": believed.get("latency_p95_ms"),
                "actual_delay_ms": round(actual_before, 2),
                "actual_delay_after_ms": measured["latency_p50_ms"],
                "fallback_reason": "; ".join(rb.reasons) if rb.applied < recommended else None,
            }
        )
    return {"config": asdict(cfg), "rows": rows, "summary": summarize(rows, cfg)}


def summarize(rows: list[dict], cfg: PoisonConfig) -> dict:
    attack = [r for r in rows if r["attacking"]] or rows[cfg.attack_start :]
    delays = [r["actual_delay_after_ms"] for r in attack]
    first_flag = next((r["tick"] for r in attack if r["verdict"] != "TRUSTED"), None)
    sent = [r for r in attack if r["verdict"] != "MISSING"]
    flagged = [r for r in sent if r["verdict"] != "TRUSTED"]
    conservative = [r for r in attack if r["guard_integrity"] not in ("TRUSTED", "UNCHECKED")]
    first_guard = next((r["tick"] for r in conservative), None)
    return {
        "arm": cfg.arm,
        "case": cfg.case,
        "ticks": len(rows),
        "mean_budget_during_attack": round(
            sum(r["budget_applied"] for r in attack) / len(attack), 2
        ),
        "max_budget_during_attack": max(r["budget_applied"] for r in attack),
        "mean_actual_delay_ms": round(sum(delays) / len(delays), 1),
        "max_actual_delay_ms": round(max(delays), 1),
        "ticks_over_1000ms": sum(1 for d in delays if d > 1000.0),
        "rejected": sum(1 for r in attack if r["verdict"] == "REJECTED"),
        "suspicious": sum(1 for r in attack if r["verdict"] == "SUSPICIOUS"),
        "trusted_during_attack": sum(1 for r in attack if r["verdict"] == "TRUSTED"),
        "detection_tick_offset": (first_flag - cfg.attack_start)
        if first_flag is not None
        else None,
        "samples_during_attack": len(sent),
        "detection_rate": round(len(flagged) / len(sent), 4) if sent else None,
        "guard_conservative_ticks": len(conservative),
        "guard_first_conservative_offset": (first_guard - cfg.attack_start)
        if first_guard is not None
        else None,
        "flagged_all_ticks": sum(1 for r in rows if r["verdict"] not in ("TRUSTED", "MISSING")),
        "fallback_ticks": sum(1 for r in attack if r["fallback_reason"]),
    }
