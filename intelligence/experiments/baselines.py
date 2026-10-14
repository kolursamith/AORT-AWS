"""Baseline inquiry budgets for controlled comparison."""


def fixed_rate(_: float, rate: float = 5.0) -> float:
    return rate


def proportional(backlog: float, fraction: float = 0.1) -> float:
    return max(0.0, backlog * fraction)
