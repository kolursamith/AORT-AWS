"""Recovery execution and measurement for the AORT banking stack.

Runs a controlled failure, executes a recovery strategy, and measures what
actually happened: RTO from a probe timeline, RPO from real backup age,
availability across the incident, and whether the double-entry ledger still
balances afterwards.

Every measurement returns None rather than a flattering default when the
honest answer is unknown - a service that never came back has no RTO, and no
backup means unbounded RPO, not zero.
"""

__version__ = "0.1.0"
