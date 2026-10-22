"""Recovery baseline policies for fair comparison with the future AORTS controller (Stage 05).

Every policy sees the same remitter-visible information through ``PolicyInput``: the decision
instant, the outstanding ambiguous transactions it has observed (with its own check history),
the subset that passes the shared safety gate right now, and the feedback of its previous
requests. No policy can see capacity, faults, beneficiary truth, customer intent or future
outcomes. Policies only *request* checks; the harness's shared gate and the simulator's own
eligibility rules decide what is released. Nothing here moves money.

Algorithms mirror Track B's reference policies (``backend/aorts/adapters/baseline_policies.py``
on feature/backend, read-only) where the semantics can be reproduced from these observations:

* naive polling: request every outstanding ambiguous transaction at every decision;
* static schedule: request a transaction when its age reaches the next scheduled offset;
* exponential backoff with full jitter: next request after U(0, min(cap, base * 2**k));
* fixed budget: a constant number per route per decision, oldest ambiguity first;
* AIMD: +1 per decision while the route's feedback is healthy, halve on any refusal,
  non-authoritative answer, timeout or slow check.

Deliberate difference from Track B: Track B's backoff draws from one shared random stream, so
its jitter depends on processing order. Here every draw comes from its own stream keyed by
(policy, seed, transaction, attempt, purpose); the distribution is the same and the result is
independent of order. Track B's numeric defaults target its 120 s deadline world; the default
presets rescale them to the research TimingPolicy (90 s first check and spacing), and the
``trackb_*`` presets keep Track B's numbers for reference.

The Track B heuristic controller (``backend/aorts/recovery/``) is NOT reproduced: see
``NOT_COMPARABLE``. No policy here is, or approximates, the AORTS controller.
"""

import hashlib
import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from contracts.v1_1 import CheckResultKind, TimingPolicy

POLICY_API_VERSION = "aorts-policy/1"


class PolicyCategory(StrEnum):
    REFERENCE = "REFERENCE"  # no recovery checks at all
    STRICT = "STRICT"  # every release obeys the research TimingPolicy and per-bank limits
    STRESS_UNCONSTRAINED = "STRESS_UNCONSTRAINED"  # isolated stress test: timing and rate off
    TIER2_EXPERIMENTAL = "TIER2_EXPERIMENTAL"  # none implemented in Stage 05
    NOT_COMPARABLE = "NOT_COMPARABLE"  # cannot be reproduced faithfully here


@dataclass(frozen=True)
class PolicyInfo:
    policy_id: str
    version: str
    category: PolicyCategory
    description: str
    mirrors: str | None  # Track B policy name whose algorithm is mirrored, if any
    max_release_per_decision: int | None  # declared per-route upper bound (None: unbounded)


@dataclass(frozen=True)
class TxnView:
    """What the remitter knows about one outstanding ambiguous transaction."""

    transaction_id: str
    route_id: str
    bank_id: str
    sent_s: float
    ambiguous_s: float
    checks_released: int
    last_check_s: float | None
    in_flight: bool
    last_result: str | None


@dataclass(frozen=True)
class CheckOutcome:
    """Feedback on one requested check. ``kind``: RELEASED, REFUSED, ANSWERED or TIMED_OUT."""

    transaction_id: str
    route_id: str
    kind: str
    at_s: float
    detail: str | None = None  # refusal reason or check result
    latency_s: float | None = None  # release to answer


@dataclass(frozen=True)
class PolicyInput:
    now_s: float
    decision_index: int
    routes: tuple[str, ...]
    outstanding: tuple[TxnView, ...]
    eligible: tuple[TxnView, ...]
    feedback: tuple[CheckOutcome, ...]
    timing: TimingPolicy


@dataclass(frozen=True)
class PolicyAction:
    """Transactions to check, in priority order. Budgets cap releases per route (None: no cap)."""

    transaction_ids: tuple[str, ...] = ()
    route_budgets: tuple[tuple[str, int], ...] = ()

    def budget(self, route_id: str) -> int | None:
        return dict(self.route_budgets).get(route_id)


class Policy(Protocol):
    info: PolicyInfo

    def decide(self, observed: PolicyInput) -> PolicyAction: ...

    def state(self) -> Mapping[str, object]: ...


def _oldest_first(views) -> list[TxnView]:
    return sorted(views, key=lambda v: (v.ambiguous_s, v.transaction_id))


def jitter(policy_id: str, seed: int, transaction_id: str, attempt: int, purpose: str) -> float:
    """Deterministic U(0, 1) draw isolated by policy, seed, transaction, attempt and purpose."""

    digest = hashlib.sha256(
        f"aorts-policy|{policy_id}|{seed}|{transaction_id}|{attempt}|{purpose}".encode()
    ).digest()
    return random.Random(int.from_bytes(digest[:8], "big")).random()  # noqa: S311 - research


# --------------------------------------------------------------------------- policies


@dataclass
class NoCheck:
    info: PolicyInfo = field(
        default=PolicyInfo(
            "no-check", "1", PolicyCategory.REFERENCE, "Never requests a status check.", None, 0
        )
    )

    def decide(self, observed: PolicyInput) -> PolicyAction:
        return PolicyAction()

    def state(self) -> Mapping[str, object]:
        return {}


@dataclass
class NaivePolling:
    """Every outstanding ambiguous transaction on every decision (Track B ``naive-polling``).

    STRESS ONLY: the harness runs it with the timing policy and per-bank limits disabled; its
    results are labelled noncompliant and are never comparable with STRICT arms.
    """

    info: PolicyInfo = field(
        default=PolicyInfo(
            "naive-polling",
            "1",
            PolicyCategory.STRESS_UNCONSTRAINED,
            "Requests every outstanding ambiguous transaction every decision.",
            "naive-polling",
            None,
        )
    )

    def decide(self, observed: PolicyInput) -> PolicyAction:
        return PolicyAction(tuple(v.transaction_id for v in _oldest_first(observed.outstanding)))

    def state(self) -> Mapping[str, object]:
        return {}


TRACKB_STATIC_SCHEDULE_S = (1.0, 3.0, 6.0, 10.0, 15.0, 21.0)


@dataclass
class StaticPolling:
    """Request when the age since ``reference`` reaches the next offset (Track B ``npci-static``).

    ``reference``: "SENT" or "AMBIGUOUS". The research preset checks as soon as the research
    TimingPolicy allows (90, 180 and 270 s after sending); the Track B preset keeps Track B's
    simulation-only offsets after ambiguity. Neither is a statement of an NPCI/RBI rule.
    """

    schedule_s: tuple[float, ...] = (90.0, 180.0, 270.0)
    reference: str = "SENT"
    info: PolicyInfo = field(
        default=PolicyInfo(
            "static-polling",
            "1",
            PolicyCategory.STRICT,
            "Fixed check offsets per transaction.",
            "npci-static",
            None,
        )
    )

    def decide(self, observed: PolicyInput) -> PolicyAction:
        chosen = []
        for v in _oldest_first(observed.outstanding):
            k = v.checks_released
            origin = v.sent_s if self.reference == "SENT" else v.ambiguous_s
            if k < len(self.schedule_s) and observed.now_s - origin >= self.schedule_s[k]:
                chosen.append(v.transaction_id)
        return PolicyAction(tuple(chosen))

    def state(self) -> Mapping[str, object]:
        return {"schedule_s": self.schedule_s, "reference": self.reference}


@dataclass
class ExponentialBackoffJitter:
    """Track B ``exp-backoff-jitter``: next request at age + U(0, min(cap, base * 2**k)).

    Age is measured from ambiguity; ``k`` is the number of checks already released. As in Track
    B, the next due time is set whenever the transaction is requested.
    """

    base_s: float = 90.0
    cap_s: float = 720.0
    seed: int = 42
    info: PolicyInfo = field(
        default=PolicyInfo(
            "exp-backoff-jitter",
            "1",
            PolicyCategory.STRICT,
            "Exponential backoff with deterministic full jitter.",
            "exp-backoff-jitter",
            None,
        )
    )
    next_due: dict[str, float] = field(default_factory=dict)

    def decide(self, observed: PolicyInput) -> PolicyAction:
        chosen = []
        for v in _oldest_first(observed.outstanding):
            age = observed.now_s - v.ambiguous_s
            due = self.next_due.get(v.transaction_id)
            if due is None or age >= due:
                k = v.checks_released
                window = min(self.cap_s, self.base_s * 2**k)
                draw = jitter(self.info.policy_id, self.seed, v.transaction_id, k, "backoff")
                self.next_due[v.transaction_id] = age + draw * window
                chosen.append(v.transaction_id)
        return PolicyAction(tuple(chosen))

    def state(self) -> Mapping[str, object]:
        return {"next_due": dict(sorted(self.next_due.items()))}


@dataclass
class FixedBudget:
    """Track B ``fixed-budget``: ``per_route`` checks per route per decision, oldest first."""

    per_route: int = 3
    info: PolicyInfo = field(
        default=PolicyInfo(
            "fixed-budget",
            "1",
            PolicyCategory.STRICT,
            "Constant checks per route per decision.",
            "fixed-budget",
            3,
        )
    )

    def decide(self, observed: PolicyInput) -> PolicyAction:
        return PolicyAction(
            tuple(v.transaction_id for v in _oldest_first(observed.eligible)),
            tuple((r, self.per_route) for r in observed.routes),
        )

    def state(self) -> Mapping[str, object]:
        return {"per_route": self.per_route}


@dataclass
class Aimd:
    """Track B ``aimd`` on per-check feedback since the previous decision.

    Healthy: no refusal, no timeout, every answer authoritative and no check latency above the
    target. Healthy -> budget + 1 (to ``ceiling``); otherwise budget / 2 (to ``floor``). The
    integer part is released, oldest ambiguity first.
    """

    start: float = 2.0
    floor: float = 1.0
    ceiling: float = 50.0
    latency_target_s: float = 0.5
    info: PolicyInfo = field(
        default=PolicyInfo(
            "aimd",
            "1",
            PolicyCategory.STRICT,
            "Additive-increase/multiplicative-decrease budget per route.",
            "aimd",
            50,
        )
    )
    budget: dict[str, float] = field(default_factory=dict)

    def decide(self, observed: PolicyInput) -> PolicyAction:
        authoritative = {
            CheckResultKind.AUTHORITATIVE_SUCCESS.value,
            CheckResultKind.AUTHORITATIVE_FAILURE.value,
        }
        budgets = []
        for route in observed.routes:
            mine = [f for f in observed.feedback if f.route_id == route]
            bad = any(
                f.kind in ("REFUSED", "TIMED_OUT")
                or (f.kind == "ANSWERED" and f.detail not in authoritative)
                or (f.latency_s is not None and f.latency_s > self.latency_target_s)
                for f in mine
            )
            b = self.budget.get(route, self.start)
            b = max(self.floor, b / 2) if bad else min(self.ceiling, b + 1)
            self.budget[route] = b
            budgets.append((route, int(b)))
        return PolicyAction(
            tuple(v.transaction_id for v in _oldest_first(observed.eligible)), tuple(budgets)
        )

    def state(self) -> Mapping[str, object]:
        return {"budget": dict(sorted(self.budget.items()))}


# ------------------------------------------------------------------------- registry


def trackb_static() -> StaticPolling:
    return StaticPolling(
        TRACKB_STATIC_SCHEDULE_S,
        "AMBIGUOUS",
        PolicyInfo(
            "static-polling-trackb",
            "1",
            PolicyCategory.STRICT,
            "Track B's simulation-only offsets after ambiguity (gated by the research policy).",
            "npci-static",
            None,
        ),
    )


def trackb_backoff(seed: int = 42) -> ExponentialBackoffJitter:
    return ExponentialBackoffJitter(
        0.5,
        20.0,
        seed,
        PolicyInfo(
            "exp-backoff-jitter-trackb",
            "1",
            PolicyCategory.STRICT,
            "Track B's numeric defaults (base 0.5 s, cap 20 s), gated by the research policy.",
            "exp-backoff-jitter",
            None,
        ),
    )


BASELINES = {
    "no-check": NoCheck,
    "naive-polling": NaivePolling,
    "static-polling": StaticPolling,
    "exp-backoff-jitter": ExponentialBackoffJitter,
    "fixed-budget": FixedBudget,
    "aimd": Aimd,
    "static-polling-trackb": trackb_static,
    "exp-backoff-jitter-trackb": trackb_backoff,
}

NOT_COMPARABLE = {
    "trackb-heuristic": (
        "Track B AortsController (backend/aorts/recovery/controller.py) sizes budgets from "
        "switch-measured worker counts and median latency per destination pool, primary-load "
        "measurements, telemetry-integrity verdicts and BudgetGuard headroom. None of these is "
        "observable from remitter-own telemetry in the Track A simulators, so a reproduction "
        "would be an approximation under a misleading name. Requirement: Track B exports those "
        "inputs as SWITCH_ROUTE-scope ValidatedRouteTelemetry plus its verdicts over /v1.1 (see "
        "contracts/v1_1/README.md handoff), or the comparison runs on Track B's live stack."
    ),
    "aorts": "The AORTS controller does not exist yet (Stages 06-08).",
}


def make_policy(policy_id: str) -> Policy:
    if policy_id in NOT_COMPARABLE:
        raise ValueError(f"{policy_id} is not comparable in Stage 05: {NOT_COMPARABLE[policy_id]}")
    return BASELINES[policy_id]()
