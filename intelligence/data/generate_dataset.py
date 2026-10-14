"""Generate reproducible synthetic transaction events for local experiments."""

import argparse
import json
import random
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from contracts.v1.models import EventType, FinancialOutcome, TransactionEvent, WorkflowState


def generate(count: int, seed: int) -> list[TransactionEvent]:
    random.seed(seed)
    events = []
    for index in range(count):
        transaction_id = f"txn-{uuid4().hex[:12]}"
        timeout = random.random() < 0.15  # noqa: S311 - reproducible simulation, not security
        events.append(
            TransactionEvent(
                event_id=f"evt-{uuid4().hex[:12]}",
                experiment_id=f"synthetic-seed-{seed}",
                transaction_id=transaction_id,
                bank_id=f"bank-{index % 3 + 1}",
                route_id=f"route-{index % 2 + 1}",
                amount_minor=random.randint(100, 100_000),  # noqa: S311 - synthetic data
                currency="INR",
                event_type=EventType.PAYMENT_TIMEOUT if timeout else EventType.PAYMENT_INITIATED,
                financial_outcome=FinancialOutcome.UNKNOWN,
                workflow_state=WorkflowState.AMBIGUOUS if timeout else WorkflowState.INITIATED,
                version=1,
                occurred_at=datetime.now(UTC),
            )
        )
    return events


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=Path, default=Path("data/synthetic/events.jsonl"))
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as output:
        for event in generate(args.count, args.seed):
            output.write(json.dumps(event.model_dump(mode="json")) + "\n")
    print(f"wrote {args.count} synthetic events to {args.output}")


if __name__ == "__main__":
    main()
