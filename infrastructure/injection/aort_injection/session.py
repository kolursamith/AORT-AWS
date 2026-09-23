"""Run one controlled failure: apply, hold, always revert, always record."""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .actions import build_apply_argv, build_revert_argv
from .events import build_event, write_events
from .targets import container_for

Docker = Callable[[Sequence[str]], tuple[int, str, str]]


class InjectionError(RuntimeError):
    """An injection could not be applied, or could not be undone."""


def _state(docker: Docker, container: str) -> str:
    code, out, err = docker(["docker", "inspect", "--format", "{{.State.Status}}", container])
    if code != 0:
        raise InjectionError(f"cannot inspect {container}: {(err or out).strip()}")
    return out.strip()


def run_injection(
    docker: Docker,
    scenario: str,
    component_id: str,
    parameters: Mapping[str, object] | None = None,
    hold_seconds: float = 30.0,
    events_path: str | Path = "injections.jsonl",
    sleep: Callable[[float], None] | None = None,
    now: Callable[[], float] = time.time,
) -> str:
    """Inject `scenario` into `component_id`, hold, then restore it.

    Returns the injection id. The revert runs even if the hold is interrupted,
    so a cancelled experiment does not leave the stack broken.
    """
    if hold_seconds < 0:
        raise ValueError(f"hold_seconds must be >= 0, got {hold_seconds}")
    pause = sleep if sleep is not None else time.sleep
    container = container_for(component_id)
    parameters = dict(parameters or {})
    injection_id = str(uuid.uuid4())

    def record(phase: str, error: str | None = None) -> None:
        write_events(events_path, [build_event(
            scenario, component_id, container, parameters, phase,
            at=now(), injection_id=injection_id, error=error)])

    def fail(message: str) -> None:
        record("failed", error=message)
        raise InjectionError(message)

    # Refuse to "inject" into something already broken: the resulting labels
    # would claim this experiment caused a failure that was already there.
    state = _state(docker, container)
    if state != "running":
        fail(f"{container} is not running (state: {state}); refusing to inject")

    apply_argv = build_apply_argv(scenario, container, parameters)
    revert_argv = build_revert_argv(scenario, container, parameters)

    code, out, err = docker(apply_argv)
    if code != 0:
        fail(f"could not apply {scenario} to {container}: {(err or out).strip()}")

    # Everything after the failure is applied runs under this try, so the
    # revert happens even if recording the event fails. Restoring the system
    # outranks bookkeeping: a disk problem must not leave a bank service down.
    try:
        record("start")
        pause(hold_seconds)
    finally:
        revert_code, revert_out, revert_err = docker(revert_argv)

    if revert_code != 0:
        fail(f"could not revert {scenario} on {container}: "
             f"{(revert_err or revert_out).strip()}")

    restored = _state(docker, container)
    if restored != "running":
        fail(f"{container} is {restored} after revert, expected running")

    record("end")
    return injection_id
