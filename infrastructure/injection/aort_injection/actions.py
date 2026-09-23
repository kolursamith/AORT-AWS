"""What each controlled failure does, and how it is undone.

Every scenario is reversible and none destroys data or containers: no `rm`,
no `kill`, no volume operations. The point is a controlled, repeatable
disturbance that telemetry can observe, not damage.
"""

from __future__ import annotations

from typing import Callable, Mapping

# A quota below this is not a throttle but an outage; above it, not a throttle
# at all on a typical dev machine.
CPU_MIN = 0.05
CPU_MAX = 8.0

# Give containers time to shut down cleanly: an unclean stop of PostgreSQL
# would confuse a data-loss measurement with an injected failure.
STOP_TIMEOUT = "10"


def _cpu_quota(parameters: Mapping[str, object]) -> str:
    raw = parameters.get("cpus")
    if raw is None:
        raise ValueError("cpu_throttle requires a 'cpus' parameter")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"cpus must be a number, got {raw!r}") from exc
    if not CPU_MIN <= value <= CPU_MAX:
        raise ValueError(f"cpus must be between {CPU_MIN} and {CPU_MAX}, got {value}")
    return format(value, "g")


Builder = Callable[[str, Mapping[str, object]], list[str]]

SCENARIOS: dict[str, tuple[Builder, Builder]] = {
    # The process is gone: requests fail outright.
    "service_stop": (
        lambda container, _p: ["docker", "stop", "--time", STOP_TIMEOUT, container],
        lambda container, _p: ["docker", "start", container],
    ),
    # Frozen, not lost: connections hang and nothing is written, but no data
    # is destroyed - the closest safe analogue of a hung service.
    "service_pause": (
        lambda container, _p: ["docker", "pause", container],
        lambda container, _p: ["docker", "unpause", container],
    ),
    # Resource exhaustion: the service still answers, slowly.
    "cpu_throttle": (
        lambda container, params: ["docker", "update", "--cpus", _cpu_quota(params), container],
        lambda container, _p: ["docker", "update", "--cpus", "0", container],  # 0 = unlimited
    ),
    # Backups stop while the bank keeps trading: availability is unaffected,
    # but RPO silently degrades. A banking-specific failure worth detecting.
    "backup_stop": (
        lambda container, _p: ["docker", "stop", "--time", STOP_TIMEOUT, container],
        lambda container, _p: ["docker", "start", container],
    ),
}


def _builders(scenario: str) -> tuple[Builder, Builder]:
    if not isinstance(scenario, str) or scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}; choose one of {sorted(SCENARIOS)}")
    return SCENARIOS[scenario]


def build_apply_argv(scenario: str, container: str, parameters: Mapping[str, object]) -> list[str]:
    return _builders(scenario)[0](container, parameters or {})


def build_revert_argv(scenario: str, container: str, parameters: Mapping[str, object]) -> list[str]:
    return _builders(scenario)[1](container, parameters or {})
