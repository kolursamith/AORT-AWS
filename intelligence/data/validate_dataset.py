"""Validate each JSONL row against the public observable-event contract."""

import argparse
import json
from pathlib import Path

from contracts.v1.models import TransactionEvent


def validate(path: Path) -> int:
    count = 0
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            try:
                TransactionEvent.model_validate(json.loads(line))
            except Exception as exc:
                raise ValueError(f"invalid row {line_number}: {exc}") from exc
            count += 1
    return count


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    print(f"validated {validate(args.path)} events")
