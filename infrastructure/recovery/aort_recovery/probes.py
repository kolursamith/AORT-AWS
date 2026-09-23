"""Health probes: is the banking service actually serving right now?"""

from __future__ import annotations

from typing import Callable

import requests

Probe = Callable[[], tuple[bool, str]]


def http_probe(url: str, timeout: float = 5.0, session=None,
               expect_status: str = "UP") -> Probe:
    """Probe an HTTP health endpoint.

    Any transport error counts as unavailable - which is the point: during an
    outage the probe must fail rather than raise and abort the experiment.
    """
    client = session or requests

    def probe() -> tuple[bool, str]:
        try:
            response = client.get(url, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - an outage is not a crash
            return False, f"{type(exc).__name__}"
        if response.status_code != 200:
            return False, f"HTTP {response.status_code}"
        try:
            status = response.json().get("status")
        except ValueError:
            return True, "200"
        return status == expect_status, str(status)

    return probe
