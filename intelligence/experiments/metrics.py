"""Policy-comparison metrics with explicit definitions (Stage 05).

``RawRun`` holds measurements exactly as collected (engine truth for evaluation, plus the
harness's own logs). ``derive`` turns them into ``RunMetrics``; nothing is re-simulated. Fewer
checks is never scored as better on its own: every check count is reported next to the
resolutions it produced.

Definitions (all counts over the run horizon, times in seconds):

* ``check_requests``: transaction IDs the policy asked for (before any gate).
* ``gate_refused`` / ``engine_refused``: requests refused by the harness's shared safety gate /
  by the simulator's own eligibility check. ``not_selected``: requests beyond the policy's own
  per-route budget (not refusals).
* ``checks_released``: checks the simulator actually enqueued (engine truth).
* ``checks_answered`` / ``checks_timed_out``: answers within the check timeout / wasted checks.
* ``new_ambiguous``: payments (originals and re-payments) whose client timeout passed without a
  response. ``resolved``: ambiguous payments later resolved (late response, check or
  re-submission). ``resolved_by_check``: resolved by an authoritative check answer.
* ``unresolved_at_end``: ambiguous payments still unresolved at the horizon.
* ``payment_latency_p95_s`` / ``p99_s``: nearest-rank quantiles of send-to-response time over
  original payments answered within the client timeout.
* ``resolution_latency_p95_s``: ambiguity-to-resolution time over resolved ambiguous payments.
* ``deadline_completion``: resolved before ``sent + deadline`` / ambiguous payments whose
  deadline falls inside the horizon (None when every deadline is censored by the horizon;
  ``deadline_censored`` counts those).
* ``recovery_goodput``: ``resolved_by_check / checks_released`` (None without checks).
* ``time_to_recovery_s``: official Stage 04 metric (estimated delay below 2 s for 60 s after the
  last capacity fault ends), worst route; None if no fault or never recovered.
* ``fairness_jain``: Jain's index over per-route resolution ratios (resolved / ambiguous) for
  routes with ambiguity; None with fewer than two such routes.
* ``invalid_releases``: released checks for a transaction the harness knew to be resolved or
  already in flight (must be 0). ``noncompliant_releases``: released checks that violated the
  research TimingPolicy or per-bank limit (must be 0 for STRICT arms; expected in stress arms).
* ``resource_cost``: ``checks_released`` (each is one unit of bottleneck work).
"""

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field


def nearest_rank(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(1, math.ceil(fraction * len(ordered))) - 1]


def jain_index(values: Sequence[float]) -> float | None:
    if len(values) < 2:
        return None
    total, squares = sum(values), sum(v * v for v in values)
    if squares == 0:
        return 1.0
    return total * total / (len(values) * squares)


@dataclass
class RouteCounts:
    new_ambiguous: int = 0
    resolved: int = 0
    unresolved_at_end: int = 0
    checks_released: int = 0
    checks_answered: int = 0
    checks_timed_out: int = 0


@dataclass
class RawRun:
    engine: str
    horizon_s: float
    routes: dict[str, RouteCounts]
    resolved_by_check: int
    payment_latencies_s: list[float]
    resolution_latencies_s: list[float]
    resolved_before_deadline: int
    deadline_eligible: int
    deadline_censored: int
    time_to_recovery_s: float | None
    check_requests: int = 0
    gate_refused: dict[str, int] = field(default_factory=dict)
    engine_refused: int = 0
    not_selected: int = 0
    invalid_releases: int = 0
    noncompliant_releases: int = 0


@dataclass(frozen=True)
class RunMetrics:
    check_requests: int
    gate_refused: int
    engine_refused: int
    not_selected: int
    checks_released: int
    checks_answered: int
    checks_timed_out: int
    new_ambiguous: int
    resolved: int
    resolved_by_check: int
    unresolved_at_end: int
    payment_latency_p95_s: float | None
    payment_latency_p99_s: float | None
    resolution_latency_p95_s: float | None
    deadline_completion: float | None
    deadline_censored: int
    recovery_goodput: float | None
    time_to_recovery_s: float | None
    fairness_jain: float | None
    per_route_resolution: tuple[tuple[str, float | None], ...]
    invalid_releases: int
    noncompliant_releases: int
    resource_cost: int


def derive(raw: RawRun) -> RunMetrics:
    routes = raw.routes
    released = sum(r.checks_released for r in routes.values())
    ratios = tuple(
        (name, (r.resolved / r.new_ambiguous) if r.new_ambiguous else None)
        for name, r in sorted(routes.items())
    )
    return RunMetrics(
        check_requests=raw.check_requests,
        gate_refused=sum(raw.gate_refused.values()),
        engine_refused=raw.engine_refused,
        not_selected=raw.not_selected,
        checks_released=released,
        checks_answered=sum(r.checks_answered for r in routes.values()),
        checks_timed_out=sum(r.checks_timed_out for r in routes.values()),
        new_ambiguous=sum(r.new_ambiguous for r in routes.values()),
        resolved=sum(r.resolved for r in routes.values()),
        resolved_by_check=raw.resolved_by_check,
        unresolved_at_end=sum(r.unresolved_at_end for r in routes.values()),
        payment_latency_p95_s=nearest_rank(raw.payment_latencies_s, 0.95),
        payment_latency_p99_s=nearest_rank(raw.payment_latencies_s, 0.99),
        resolution_latency_p95_s=nearest_rank(raw.resolution_latencies_s, 0.95),
        deadline_completion=(
            raw.resolved_before_deadline / raw.deadline_eligible if raw.deadline_eligible else None
        ),
        deadline_censored=raw.deadline_censored,
        recovery_goodput=raw.resolved_by_check / released if released else None,
        time_to_recovery_s=raw.time_to_recovery_s,
        fairness_jain=jain_index([v for _, v in ratios if v is not None]),
        per_route_resolution=ratios,
        invalid_releases=raw.invalid_releases,
        noncompliant_releases=raw.noncompliant_releases,
        resource_cost=released,
    )


def canonical_json(value) -> str:
    """Stable serialization: sorted keys, no whitespace, floats rounded to 9 places."""

    def clean(x):
        if isinstance(x, float):
            return round(x, 9)
        if isinstance(x, dict):
            return {str(k): clean(v) for k, v in x.items()}
        if isinstance(x, list | tuple):
            return [clean(v) for v in x]
        return x

    if hasattr(value, "__dataclass_fields__"):
        value = asdict(value)
    return json.dumps(clean(value), sort_keys=True, separators=(",", ":"))


def sha256_json(value) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()
