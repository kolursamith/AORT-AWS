"""Deadline-aware, deterministic ranking of ambiguous transactions for status checks (Stage 08).

``pool_from_twin`` reads the Transaction Twin (projections and its pure Tier 1 eligibility) and
keeps only transactions that are eligible now or will become eligible later: terminal,
quarantined, reconciliation, in-flight, limit-reached, window-closed and indeterminate ones
never enter the pool. ``rank_pool`` orders the pool; ``select_for_dispatch`` takes the first
``n`` that are eligible at the decision instant, honouring per-bank quotas. Nothing here reads
hidden truth or changes any state: the ranking key uses only projection facts and the
remitter's own check evidence.

Ranking key (deadline-aware, lexicographic, ascending; ties end on transaction_id):

1. ``deadline_escalated`` first (the deadline is inside the policy's escalation window);
2. earlier deadline first (unknown deadlines last);
3. higher expected value of information first (``candidate_benefit``: UNKNOWN answers and
   congested sends are discounted);
4. fewer counted checks used first (more remaining allowance);
5. older ambiguity first;
6. earlier next permitted inquiry time first.

``deadline_ranking=False`` (ablation) ranks by age only: older ambiguity, then eligibility
time, then id. Fairness: after sorting, banks are interleaved round-robin in the order of their
best-ranked transaction. A v1.1 route has one remitter bank, so the interleave only matters
when a caller ranks a pool spanning routes; per-bank quotas apply in every case.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from intelligence.controller.lookahead import CheckCandidate, CheckEvidence, candidate_benefit
from intelligence.twin.transaction_twin import EligibilityStatus, TransactionTwinRegistry

_FUTURE = datetime.max.replace(tzinfo=None)
_POOL_STATUSES = frozenset({EligibilityStatus.ELIGIBLE, EligibilityStatus.NOT_YET_ELIGIBLE})


@dataclass(frozen=True)
class Candidate:
    """Legacy starter candidate (kept for the /v1 path and its tests)."""

    transaction_id: str
    deadline: datetime
    attempts: int = 0


def rank(candidates: list[Candidate]) -> list[Candidate]:
    return sorted(candidates, key=lambda item: (item.deadline, item.attempts, item.transaction_id))


@dataclass(frozen=True, slots=True)
class PoolEntry:
    """One rankable transaction: the forecast's view plus ranking-only facts."""

    candidate: CheckCandidate
    ambiguity_created_at: datetime | None
    deadline_escalated: bool

    @property
    def transaction_id(self) -> str:
        return self.candidate.transaction_id


def pool_from_twin(
    twin: TransactionTwinRegistry,
    experiment_id: str,
    route_id: str,
    now: datetime,
    evidence: Mapping[str, CheckEvidence] | None = None,
    congested_at: Callable[[datetime], bool | None] | None = None,
) -> tuple[PoolEntry, ...]:
    """Ambiguous transactions on one route that are, or will become, eligible for a check."""

    evidence = evidence or {}
    out = []
    for txn in twin.transaction_ids(experiment_id):
        projection = twin.snapshot(experiment_id, txn)
        if projection.route_id != route_id:
            continue
        verdict = twin.eligibility(experiment_id, txn, now)
        if verdict.status not in _POOL_STATUSES or verdict.eligible_at is None:
            continue
        sent = projection.initiated_at
        out.append(
            PoolEntry(
                candidate=CheckCandidate(
                    experiment_id=experiment_id,
                    route_id=route_id,
                    transaction_id=txn,
                    version=projection.version,
                    eligible_at=verdict.eligible_at,
                    checks_used=projection.checks_used,
                    deadline_at=projection.deadline_at,
                    evidence=evidence.get(txn, CheckEvidence.NO_CHECK_YET),
                    sent_while_congested=congested_at(sent) if congested_at else None,
                    remitter_bank_id=projection.remitter_bank_id,
                ),
                ambiguity_created_at=projection.ambiguity_created_at,
                deadline_escalated=projection.deadline_escalated,
            )
        )
    return tuple(out)


def _naive(value: datetime | None) -> datetime:
    return _FUTURE if value is None else value.replace(tzinfo=None)


def rank_pool(
    pool: Sequence[PoolEntry],
    benefit_base: float,
    benefit_config,
    *,
    deadline_ranking: bool = True,
) -> tuple[PoolEntry, ...]:
    """Deterministic order; see the module docstring for the key."""

    def key(entry: PoolEntry):
        c = entry.candidate
        if not deadline_ranking:
            return (_naive(entry.ambiguity_created_at), _naive(c.eligible_at), c.transaction_id)
        return (
            0 if entry.deadline_escalated else 1,
            _naive(c.deadline_at),
            -round(candidate_benefit(c, benefit_base, benefit_config), 9),
            c.checks_used,
            _naive(entry.ambiguity_created_at),
            _naive(c.eligible_at),
            c.transaction_id,
        )

    ordered = sorted(pool, key=key)
    banks: dict[str, list[PoolEntry]] = {}
    for entry in ordered:
        banks.setdefault(entry.candidate.remitter_bank_id or "", []).append(entry)
    if len(banks) <= 1:
        return tuple(ordered)
    queues = list(banks.values())  # dict order = order of each bank's best transaction
    out: list[PoolEntry] = []
    while any(queues):
        for queue in queues:
            if queue:
                out.append(queue.pop(0))
    return tuple(out)


def select_for_dispatch(
    ranked: Sequence[PoolEntry],
    now: datetime,
    limit: int,
    bank_quota: Mapping[str, int] | None = None,
) -> tuple[PoolEntry, ...]:
    """The first ``limit`` entries eligible at ``now``; each id once; per-bank quotas honoured."""

    used: dict[str, int] = {}
    seen: set[str] = set()
    chosen: list[PoolEntry] = []
    for entry in ranked:
        if len(chosen) >= limit:
            break
        c = entry.candidate
        if c.transaction_id in seen or c.eligible_at > now:
            continue
        bank = c.remitter_bank_id or ""
        if bank_quota is not None and used.get(bank, 0) >= bank_quota.get(bank, 0):
            continue
        seen.add(c.transaction_id)
        used[bank] = used.get(bank, 0) + 1
        chosen.append(entry)
    return tuple(chosen)
