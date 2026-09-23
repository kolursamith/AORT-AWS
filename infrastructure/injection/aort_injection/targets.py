"""Which components may be perturbed.

An allow-list, not a convenience. Injection that could reach an arbitrary
container could stop Prometheus or the collector, and an experiment that
destroys its own observation produces no evidence.
"""

from __future__ import annotations

CONTAINERS: dict[str, str] = {
    "fineract": "aort-fineract",    # the core banking application
    "postgres": "aort-postgres",    # the ledger database
    "backup": "aort-db-backup",     # the ledger backup sidecar (RPO)
}


def container_for(component_id: str) -> str:
    """The container implementing a component, refusing anything unlisted."""
    if not isinstance(component_id, str) or component_id not in CONTAINERS:
        raise ValueError(
            f"{component_id!r} is not an injectable component; "
            f"choose one of {sorted(CONTAINERS)}"
        )
    return CONTAINERS[component_id]
