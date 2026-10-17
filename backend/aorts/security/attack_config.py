"""Safety rails for the attack simulators.

Attacks may only target localhost, the in-process test server, the Docker
Compose service names, or hosts explicitly listed in ``AORTS_ATTACK_ALLOWED_HOSTS``
for an isolated test deployment. They never target real banks, NPCI/UPI or any
public system. Rates and durations are capped.
"""

from __future__ import annotations

from urllib.parse import urlsplit

MAX_ATTACK_RPS = 500.0
MAX_DURATION_S = 120.0


class AttackNotAllowed(Exception):
    pass


def assert_isolated_target(base_url: str, allowed_hosts: set[str]) -> None:
    host = urlsplit(base_url).hostname or ""
    if host not in allowed_hosts:
        raise AttackNotAllowed(f"refusing to attack '{host}': not in AORTS_ATTACK_ALLOWED_HOSTS")


def clamp_rate(rps: float, duration_s: float) -> tuple[float, float]:
    return min(max(rps, 0.1), MAX_ATTACK_RPS), min(max(duration_s, 0.5), MAX_DURATION_S)
