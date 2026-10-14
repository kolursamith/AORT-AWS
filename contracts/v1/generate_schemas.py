"""Regenerate checked-in JSON Schemas: python -m contracts.v1.generate_schemas."""

import json
from pathlib import Path

from contracts.v1.models import (
    RecoveryDecision,
    RouteTelemetry,
    StatusCheckResult,
    TransactionEvent,
)

SCHEMAS = {
    "transaction.schema.json": TransactionEvent,
    "telemetry.schema.json": RouteTelemetry,
    "decision.schema.json": RecoveryDecision,
    "status_result.schema.json": StatusCheckResult,
}


def main() -> None:
    root = Path(__file__).parent
    for filename, model in SCHEMAS.items():
        (root / filename).write_text(
            json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
