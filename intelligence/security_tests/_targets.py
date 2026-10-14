"""Hard safety boundary for local attack simulations."""

import ipaddress
import os
from urllib.parse import urlparse


def require_isolated_target(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.hostname
    allowed = set(os.getenv("AORT_ALLOWED_ATTACK_HOSTS", "localhost,127.0.0.1").split(","))
    if not host or host not in allowed:
        raise ValueError(f"refusing non-allowlisted attack target: {host!r}")
    try:
        address = ipaddress.ip_address(host)
        if not (address.is_loopback or address.is_private):
            raise ValueError("attack target must resolve to a loopback/private literal")
    except ValueError:
        # Named Docker services are permitted only by the explicit allowlist.
        if host not in {"localhost", "switch", "beneficiary", "remitter"}:
            raise ValueError("named attack target is not a known local mock") from None
    return url
