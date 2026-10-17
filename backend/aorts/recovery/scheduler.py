"""Ambiguous-transaction scheduler (AORTS, Track B implementation).

Score in [0, 1] (higher = check sooner), every component recorded for explainability:

* urgency   (w=0.40): ``1 - time_left / deadline_window``  - closer to its (simulated) deadline first;
* age       (w=0.25): ``min(1, ambiguous_age / deadline_window)``;
* evidence  (w=0.20): 1.0 if never checked (the first check is the most informative), 0.6 after one
                      inconclusive check, 0.3 after more - repeated inconclusive answers suggest the
                      route is still degraded;
* retry     (w=0.15): ``1 / (1 + checks_started)`` - avoids hammering the same transaction.

Starvation guard: a transaction not checked for ``starvation_s`` (or never checked and ambiguous for
that long) is FORCED to the front, regardless of score, so nothing is delayed indefinitely.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

W_URGENCY, W_AGE, W_EVIDENCE, W_RETRY = 0.40, 0.25, 0.20, 0.15


@dataclass
class ScheduledTxn:
    transaction_id: str
    route_id: str
    score: float
    forced: bool
    components: dict

    def as_dict(self) -> dict:
        return {
            "transaction_id": self.transaction_id,
            "route_id": self.route_id,
            "score": round(self.score, 4),
            "forced": self.forced,
            "components": {k: round(v, 3) for k, v in self.components.items()},
        }


class PriorityScheduler:
    def __init__(self, starvation_s: float = 8.0, default_window_s: float = 120.0):
        self.starvation_s = starvation_s
        self.default_window = default_window_s

    def rank(self, candidates: list[dict], twins: dict, now: datetime) -> list[ScheduledTxn]:
        out = []
        for c in candidates:
            tid = c["transaction_id"]
            t = twins.get(tid)
            since = _dt(c.get("ambiguous_since")) or (t.ambiguous_since if t else None) or now
            deadline = _dt(c.get("deadline_at")) or (t.deadline_at if t else None)
            created = t.created_at if t and t.created_at else since
            window = (
                (deadline - created).total_seconds() if deadline else self.default_window
            ) or self.default_window
            left = (deadline - now).total_seconds() if deadline else window
            age = max(0.0, (now - since).total_seconds())
            checks = max(int(c.get("checks_used") or 0), t.checks_started if t else 0)
            inconclusive = t.inconclusive_checks if t else 0
            last_check = t.last_check_at if t and t.last_check_at else None
            urgency = min(1.0, max(0.0, 1 - left / window))
            age_n = min(1.0, age / window)
            evidence = 1.0 if inconclusive == 0 else (0.6 if inconclusive == 1 else 0.3)
            retry = 1.0 / (1 + checks)
            score = W_URGENCY * urgency + W_AGE * age_n + W_EVIDENCE * evidence + W_RETRY * retry
            idle = (now - last_check).total_seconds() if last_check else age
            forced = idle >= self.starvation_s
            out.append(
                ScheduledTxn(
                    tid,
                    c["route_id"],
                    score,
                    forced,
                    {
                        "urgency": urgency,
                        "age": age_n,
                        "evidence": evidence,
                        "retry": retry,
                        "seconds_left": left,
                        "idle_s": idle,
                    },
                )
            )
        out.sort(key=lambda x: (not x.forced, -x.score, x.transaction_id))
        return out


def _dt(value) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)
