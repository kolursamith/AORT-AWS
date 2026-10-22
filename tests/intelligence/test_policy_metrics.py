"""Stage 05: metric definitions, computed on hand-built raw runs with known answers."""

import pytest

from intelligence.experiments.metrics import (
    RawRun,
    RouteCounts,
    canonical_json,
    derive,
    jain_index,
    nearest_rank,
    sha256_json,
)


def raw(**overrides) -> RawRun:
    base = dict(
        engine="DES",
        horizon_s=2400.0,
        routes={
            "A1-B": RouteCounts(
                new_ambiguous=10,
                resolved=8,
                unresolved_at_end=2,
                checks_released=20,
                checks_answered=12,
                checks_timed_out=7,
            ),
            "A2-C": RouteCounts(
                new_ambiguous=5,
                resolved=5,
                unresolved_at_end=0,
                checks_released=4,
                checks_answered=4,
                checks_timed_out=0,
            ),
        },  # fmt: skip
        resolved_by_check=6,
        payment_latencies_s=[float(i) for i in range(1, 101)],
        resolution_latencies_s=[10.0, 20.0, 30.0],
        resolved_before_deadline=3,
        deadline_eligible=4,
        deadline_censored=11,
        time_to_recovery_s=64.0,
        check_requests=40,
        gate_refused={"timing_policy": 9, "in_flight": 3},
        engine_refused=1,
        not_selected=5,
    )
    return RawRun(**{**base, **overrides})


def test_counts_and_derived_ratios():
    m = derive(raw())
    assert (m.checks_released, m.checks_answered, m.checks_timed_out) == (24, 16, 7)
    assert (m.new_ambiguous, m.resolved, m.unresolved_at_end) == (15, 13, 2)
    assert (m.gate_refused, m.engine_refused, m.not_selected, m.check_requests) == (12, 1, 5, 40)
    assert m.recovery_goodput == pytest.approx(6 / 24)
    assert m.deadline_completion == pytest.approx(0.75) and m.deadline_censored == 11
    assert m.resource_cost == 24 and m.time_to_recovery_s == 64.0
    assert m.per_route_resolution == (("A1-B", 0.8), ("A2-C", 1.0))
    assert m.fairness_jain == pytest.approx((1.8**2) / (2 * (0.64 + 1.0)))


def test_latency_quantiles_use_nearest_rank():
    m = derive(raw())
    assert (m.payment_latency_p95_s, m.payment_latency_p99_s) == (95.0, 99.0)
    assert m.resolution_latency_p95_s == 30.0
    assert nearest_rank([], 0.95) is None and nearest_rank([7.0], 0.99) == 7.0


def test_undefined_metrics_are_none_not_zero():
    empty = raw(
        routes={"A1-B": RouteCounts()},
        resolved_by_check=0,
        payment_latencies_s=[],
        resolution_latencies_s=[],
        resolved_before_deadline=0,
        deadline_eligible=0,
        time_to_recovery_s=None,
    )
    m = derive(empty)
    assert m.recovery_goodput is None and m.deadline_completion is None
    assert m.payment_latency_p95_s is None and m.fairness_jain is None


def test_fewer_checks_is_not_better_by_itself():
    lazy = derive(raw(routes={"A1-B": RouteCounts(new_ambiguous=10, resolved=0,
                                                   unresolved_at_end=10)},
                      resolved_by_check=0))  # fmt: skip
    assert lazy.resource_cost == 0 and lazy.unresolved_at_end == 10
    assert lazy.recovery_goodput is None  # no claim of efficiency without checks


def test_jain_index_edges():
    assert jain_index([0.5, 0.5, 0.5]) == pytest.approx(1.0)
    assert jain_index([1.0, 0.0]) == pytest.approx(0.5)
    assert jain_index([0.0, 0.0]) == 1.0
    assert jain_index([1.0]) is None


def test_serialization_is_canonical_and_stable():
    a = canonical_json({"b": 1.0000000001, "a": [1, 2], "c": None})
    assert a == '{"a":[1,2],"b":1.0,"c":null}'
    m = derive(raw())
    assert canonical_json(m) == canonical_json(derive(raw()))
    assert sha256_json(m) == sha256_json(derive(raw())) and len(sha256_json(m)) == 64
