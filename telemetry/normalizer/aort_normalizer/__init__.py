"""AORT Layer 1a - telemetry normalization.

Reads the Phase 3 Prometheus and emits I1 observation records
(contracts/i1-observation.provisional-0.1.schema.json) for the banking-system
components the operational digital twin models.

Every value comes from a real Prometheus query. When a signal is absent,
NaN or ambiguous, the record says so through its ``quality`` field and carries
no value - nothing is filled in or estimated.
"""

__version__ = "0.1.0"
