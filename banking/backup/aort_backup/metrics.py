"""Reading the backup-state metrics the sidecar writes for node-exporter."""

from __future__ import annotations

import math

LAST_SUCCESS = "aort_backup_last_success_timestamp_seconds"


def parse_textfile(text: str) -> dict[str, float]:
    """Parse Prometheus text-format samples into {series: value}.

    Comments and blank lines are ignored. A malformed sample line raises,
    rather than being skipped, so a corrupted file is loud instead of silently
    producing an incomplete picture of backup state.
    """
    metrics: dict[str, float] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2:
            raise ValueError(f"malformed sample line: {raw!r}")
        name, value = parts
        try:
            metrics[name] = float(value)
        except ValueError as exc:
            raise ValueError(f"non-numeric value in sample line: {raw!r}") from exc
    return metrics


def backup_age_seconds(metrics: dict[str, float], now: float) -> float | None:
    """Seconds since the last successful backup - the RPO input.

    Returns None when no backup has ever succeeded, and also when the recorded
    timestamp is corrupted (NaN/Inf). Neither may be reported as 0, which would
    claim a perfectly fresh backup exists, nor as NaN, which the recovery
    optimizer could mistake for a real number.
    """
    last_success = metrics.get(LAST_SUCCESS)
    if last_success is None or not math.isfinite(last_success) or not math.isfinite(now):
        return None
    # Small negative ages come from clock differences, not from a backup taken
    # in the future.
    return max(0.0, now - last_success)
