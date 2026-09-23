"""One end-to-end recovery experiment.

Baseline the ledger, inject a controlled failure, watch the outage, execute the
recovery strategy, and measure what actually happened. The result is an I6
record linked to the I4 injection that caused it.

This closes the research chain: telemetry -> failure -> recovery action ->
measured RTO, RPO, availability and transaction integrity.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from aort_injection.actions import build_apply_argv
from aort_injection.events import build_event, write_events
from aort_injection.targets import container_for

from .measure import (
    Probe,
    availability_ratio,
    recovery_moment,
    rpo_seconds,
    time_to_recover,
    transaction_integrity,
)
from .outcomes import build_outcome, write_outcome
from .strategies import build_recovery_argv, strategy_for_scenario

Docker = Callable[[Sequence[str]], tuple[int, str, str]]


class RecoveryError(RuntimeError):
    """The experiment could not be run, or the recovery command failed."""


def _steps(duration: float, interval: float) -> int:
    return max(1, round(duration / interval))


def run_recovery_experiment(
    *,
    docker: Docker,
    scenario: str,
    component_id: str,
    probe: Callable[[], tuple[bool, str]],
    ledger: Any,
    outcomes_path: str | Path,
    events_path: str | Path,
    backup_at: float | None = None,
    failure_seconds: float = 30.0,
    probe_interval: float = 2.0,
    recovery_timeout: float = 180.0,
    sleep: Callable[[float], None] | None = None,
    now: Callable[[], float] = time.time,
    predicted_rto_seconds: float | None = None,
    predicted_rpo_seconds: float | None = None,
) -> dict[str, Any]:
    pause = sleep if sleep is not None else time.sleep

    # Validate before touching anything: a bad experiment must not perturb a
    # running bank.
    strategy = strategy_for_scenario(scenario)
    container = container_for(component_id)
    if failure_seconds < 0 or recovery_timeout < 0:
        raise ValueError("durations must be >= 0")
    if probe_interval <= 0:
        raise ValueError("probe_interval must be > 0")

    experiment_id = str(uuid.uuid4())
    injection_id = str(uuid.uuid4())
    rows_before: Mapping[str, int] = ledger.row_counts()

    code, out, err = docker(["docker", "inspect", "--format", "{{.State.Status}}", container])
    if code != 0:
        raise RecoveryError(f"cannot inspect {container}: {(err or out).strip()}")
    if out.strip() != "running":
        raise RecoveryError(
            f"{container} is not running (state: {out.strip()}); refusing to experiment")

    apply_argv = build_apply_argv(scenario, container, {})
    recovery_argv = build_recovery_argv(strategy, container)

    failure_started_at = now()
    code, out, err = docker(apply_argv)
    if code != 0:
        raise RecoveryError(f"could not inject {scenario}: {(err or out).strip()}")

    probes: list[Probe] = []
    # The recovery runs in `finally`: if probing fails the stack must still be
    # restored, never left in the injected failure state.
    try:
        write_events(events_path, [build_event(
            scenario, component_id, container, {}, "start",
            at=now(), injection_id=injection_id)])
        for _ in range(_steps(failure_seconds, probe_interval)):
            at = now()
            ok, detail = probe()
            probes.append(Probe(at=at, ok=ok, detail=detail))
            pause(probe_interval)
    finally:
        recovery_started_at = now()
        revert_code, revert_out, revert_err = docker(recovery_argv)

    if revert_code != 0:
        raise RecoveryError(
            f"recovery {strategy} failed on {container}: {(revert_err or revert_out).strip()}")

    write_events(events_path, [build_event(
        scenario, component_id, container, {}, "end",
        at=now(), injection_id=injection_id)])

    for _ in range(_steps(recovery_timeout, probe_interval)):
        at = now()
        ok, detail = probe()
        probes.append(Probe(at=at, ok=ok, detail=detail))
        if time_to_recover(probes, failure_started_at) is not None:
            break
        pause(probe_interval)

    rows_after: Mapping[str, int] = ledger.row_counts()
    debits, credits = ledger.ledger_totals()

    outcome = build_outcome(
        experiment_id=experiment_id,
        injection_id=injection_id,
        scenario=scenario,
        component_id=component_id,
        strategy=strategy,
        failure_started_at=failure_started_at,
        recovery_started_at=recovery_started_at,
        recovered_at=recovery_moment(probes, failure_started_at),
        rto_seconds=time_to_recover(probes, failure_started_at),
        rpo_seconds=rpo_seconds(failure_started_at, backup_at),
        availability_ratio=availability_ratio(probes),
        probes_total=len(probes),
        probes_ok=sum(1 for p in probes if p.ok),
        transaction_integrity=transaction_integrity(debits, credits),
        ledger_rows_before=rows_before,
        ledger_rows_after=rows_after,
        predicted_rto_seconds=predicted_rto_seconds,
        predicted_rpo_seconds=predicted_rpo_seconds,
    )
    write_outcome(outcomes_path, outcome)
    return outcome
