from datetime import UTC, datetime, timedelta

import pytest

from intelligence.controller.fallback import safe_budget
from intelligence.controller.invariant_policy import validate_recommendation
from intelligence.controller.ranker import Candidate, rank


def test_low_confidence_budget_is_conservative() -> None:
    assert safe_budget(100, 0.1) == 10
    assert safe_budget(100, 1.0) == 25


def test_controller_cannot_recommend_financial_operation() -> None:
    with pytest.raises(ValueError):
        validate_recommendation("reverse")


def test_rank_uses_deadline_then_attempts() -> None:
    now = datetime.now(UTC)
    candidates = [
        Candidate("later", now + timedelta(hours=1)),
        Candidate("retried", now, attempts=2),
        Candidate("first", now, attempts=0),
    ]
    assert [item.transaction_id for item in rank(candidates)] == ["first", "retried", "later"]
