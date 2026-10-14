"""Local-only fault definitions."""

from dataclasses import dataclass


@dataclass(frozen=True)
class CapacityFault:
    capacity_multiplier: float = 0.35

    def apply(self, capacity_rps: float) -> float:
        return max(0.0, capacity_rps * self.capacity_multiplier)
