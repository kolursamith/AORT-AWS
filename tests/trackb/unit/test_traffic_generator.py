"""Regression: two traffic runs with the same seed must not share idempotency keys."""

from backend.aorts.control.traffic import TrafficConfig, TrafficGenerator


def test_same_seed_runs_use_distinct_idempotency_keys_but_same_workload():
    a, b = (TrafficGenerator(client=None, cfg=TrafficConfig(seed=7)) for _ in range(2))
    bodies_a = [a._body() for _ in range(20)]
    bodies_b = [b._body() for _ in range(20)]
    assert not {x["idempotency_key"] for x in bodies_a} & {x["idempotency_key"] for x in bodies_b}
    strip = lambda bs: [{k: v for k, v in x.items() if k != "idempotency_key"} for x in bs]  # noqa: E731
    assert strip(bodies_a) == strip(bodies_b)  # reproducible accounts/amounts/destinations
    assert all(8 <= len(x["idempotency_key"]) <= 80 for x in bodies_a)
