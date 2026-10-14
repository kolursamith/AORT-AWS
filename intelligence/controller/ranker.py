"""Deadline-aware ranking that is deterministic and easy to unit test."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Candidate:
    transaction_id: str
    deadline: datetime
    attempts: int = 0


def rank(candidates: list[Candidate]) -> list[Candidate]:
    return sorted(candidates, key=lambda item: (item.deadline, item.attempts, item.transaction_id))
