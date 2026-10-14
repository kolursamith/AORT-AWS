"""Dependency-free discrete-time demonstration of recovery feedback."""

from dataclasses import dataclass

from intelligence.simulation.faults import CapacityFault
from intelligence.simulation.scenarios import HACKATHON_SCENARIO, Scenario


@dataclass(frozen=True)
class Tick:
    second: int
    capacity_rps: float
    check_rps: float
    ambiguous_total: float


def run(scenario: Scenario = HACKATHON_SCENARIO) -> list[Tick]:
    fault = CapacityFault()
    ambiguous = 0.0
    check_rps = 0.0
    ticks = []
    for second in range(scenario.duration_seconds):
        capacity = scenario.route_capacity_rps
        if scenario.fault_at_second is not None and second >= scenario.fault_at_second:
            capacity = fault.apply(capacity)
        payment_capacity = max(0.0, capacity - check_rps)
        new_ambiguous = max(0.0, scenario.payment_rps - payment_capacity)
        resolved = min(ambiguous, check_rps)
        ambiguous = max(0.0, ambiguous + new_ambiguous - resolved)
        # Conservative standalone controller: never use more than 10% of capacity.
        check_rps = min(ambiguous, capacity * 0.10)
        ticks.append(Tick(second, capacity, check_rps, ambiguous))
    return ticks


def main() -> None:
    ticks = run()
    print("second capacity_rps check_rps ambiguous")
    for tick in ticks:
        print(
            f"{tick.second:>6} {tick.capacity_rps:>12.1f} "
            f"{tick.check_rps:>9.1f} {tick.ambiguous_total:>9.1f}"
        )


if __name__ == "__main__":
    main()
