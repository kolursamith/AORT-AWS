"""Optimization facade; research solvers can replace this without changing the API."""

from intelligence.controller.lookahead import choose_budget


def optimize(capacity_rps: float, payment_rps: float) -> float:
    return choose_budget(capacity_rps, payment_rps, [0.0, 1.0, 2.0, 5.0, 10.0])
