"""The measured recovery outcome record (I6)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = "i6-recovery-provisional-0.1"


def format_timestamp(epoch_seconds: float) -> str:
    moment = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def build_outcome(
    *,
    experiment_id: str,
    injection_id: str,
    scenario: str,
    component_id: str,
    strategy: str,
    failure_started_at: float,
    recovery_started_at: float,
    recovered_at: float | None,
    rto_seconds: float | None,
    rpo_seconds: float | None,
    availability_ratio: float,
    probes_total: int,
    probes_ok: int,
    transaction_integrity: str,
    ledger_rows_before: Mapping[str, int],
    ledger_rows_after: Mapping[str, int],
    predicted_rto_seconds: float | None = None,
    predicted_rpo_seconds: float | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    if not 0.0 <= availability_ratio <= 1.0:
        raise ValueError(f"availability_ratio must be within [0, 1], got {availability_ratio}")
    if probes_ok > probes_total:
        raise ValueError(f"probes_ok ({probes_ok}) exceeds probes_total ({probes_total})")

    # "Recovered" means observed healthy again, not merely "we ran a command".
    recovered = recovered_at is not None and rto_seconds is not None
    return {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": experiment_id,
        "injection_id": injection_id,
        "scenario": scenario,
        "component_id": component_id,
        "strategy": strategy,
        "failure_started_at": format_timestamp(failure_started_at),
        "recovery_started_at": format_timestamp(recovery_started_at),
        "recovered_at": format_timestamp(recovered_at) if recovered else None,
        "recovered": recovered,
        "rto_seconds": round(rto_seconds, 3) if recovered else None,
        "rpo_seconds": None if rpo_seconds is None else round(rpo_seconds, 3),
        "availability_ratio": round(availability_ratio, 4),
        "probes_total": int(probes_total),
        "probes_ok": int(probes_ok),
        "transaction_integrity": transaction_integrity,
        "ledger_rows_before": {str(k): int(v) for k, v in dict(ledger_rows_before).items()},
        "ledger_rows_after": {str(k): int(v) for k, v in dict(ledger_rows_after).items()},
        # Left for the recovery optimizer (Owner A) so predicted-versus-actual
        # can be evaluated later.
        "predicted_rto_seconds": predicted_rto_seconds,
        "predicted_rpo_seconds": predicted_rpo_seconds,
        "notes": notes,
    }


def write_outcome(path: str | Path, outcome: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(outcome, allow_nan=False) + "\n")
