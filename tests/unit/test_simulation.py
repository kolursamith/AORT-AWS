from intelligence.simulation.engine import run
from intelligence.simulation.scenarios import HACKATHON_SCENARIO


def test_simulation_is_reproducible_and_fault_increases_backlog() -> None:
    first = run()
    assert first == run()
    fault_at = HACKATHON_SCENARIO.fault_at_second
    assert fault_at is not None
    assert first[-1].ambiguous_total > first[fault_at - 1].ambiguous_total
