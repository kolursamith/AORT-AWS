"""Recovery strategies: what is actually done to bring a component back.

Owner A's optimizer decides *which* strategy; this module executes it. The
vocabulary is deliberately small and matches the I6 contract.
"""

from __future__ import annotations

from typing import Callable

RECOVERY_FOR_SCENARIO: dict[str, str] = {
    "service_stop": "restart_service",
    "service_pause": "unpause_service",
    "backup_stop": "restart_service",
    # cpu_throttle is absent on purpose: resetting a CPU quota is not one of
    # the three modelled strategies, and mapping it to a restart would
    # misreport what recovery was performed.
}

DOCKER_RECOVERY: dict[str, Callable[[str], list[str]]] = {
    "restart_service": lambda container: ["docker", "start", container],
    "unpause_service": lambda container: ["docker", "unpause", container],
}


def strategy_for_scenario(scenario: str) -> str:
    if not isinstance(scenario, str) or scenario not in RECOVERY_FOR_SCENARIO:
        raise ValueError(
            f"no recovery strategy defined for scenario {scenario!r}; "
            f"known: {sorted(RECOVERY_FOR_SCENARIO)}"
        )
    return RECOVERY_FOR_SCENARIO[scenario]


def build_recovery_argv(strategy: str, container: str) -> list[str]:
    """The command that performs a container-level recovery."""
    if strategy not in DOCKER_RECOVERY:
        # restore_from_backup runs through aort_backup, not a docker one-liner.
        raise ValueError(f"{strategy!r} is not a container-level recovery strategy")
    return DOCKER_RECOVERY[strategy](container)
