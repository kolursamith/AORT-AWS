"""Observable synthetic dataset records; ground truth is deliberately separate."""

from dataclasses import dataclass


@dataclass(frozen=True)
class HiddenGroundTruth:
    transaction_id: str
    beneficiary_credited: bool
    injected_fault: str | None = None
