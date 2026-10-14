"""Financial-integrity guardrails for controller recommendations."""

from enum import StrEnum


class Recommendation(StrEnum):
    INQUIRE = "inquire"
    WAIT = "wait"
    ESCALATE = "escalate"


def validate_recommendation(value: str) -> Recommendation:
    try:
        return Recommendation(value)
    except ValueError as exc:
        raise ValueError("controller may only recommend inquire, wait, or escalate") from exc
