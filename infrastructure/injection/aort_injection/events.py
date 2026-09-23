"""Injection events: the ground truth for every controlled failure.

Telemetry shows that something changed. These records say what was done, to
what, and exactly when - which is what turns an experiment into labelled data.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

SCHEMA_VERSION = "i4-injection-provisional-0.1"


def format_timestamp(epoch_seconds: float) -> str:
    """UTC ISO-8601 with millisecond precision and a trailing Z."""
    moment = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def build_event(
    scenario: str,
    component_id: str,
    container: str,
    parameters: Mapping[str, Any] | None,
    phase: str,
    at: float,
    injection_id: str,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "injection_id": injection_id,
        "phase": phase,
        "scenario": scenario,
        "component_id": component_id,
        "container": container,
        "parameters": {str(k): str(v) for k, v in (parameters or {}).items()},
        "at": format_timestamp(at),
        "error": error,
    }


def write_events(path: str | Path, events: Iterable[Mapping[str, Any]]) -> None:
    """Append events as JSON Lines, so a run builds a labelled timeline."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, allow_nan=False) + "\n")
