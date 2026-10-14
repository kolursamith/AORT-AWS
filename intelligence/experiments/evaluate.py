"""Evaluation entry point kept separate from experiment generation."""

import json
from pathlib import Path


def maximum_backlog(path: Path) -> float:
    rows = json.loads(path.read_text(encoding="utf-8"))
    return max((float(row["ambiguous_total"]) for row in rows), default=0.0)
