"""Small counterfactual scorer for candidate inquiry budgets."""


def choose_budget(capacity_rps: float, payment_rps: float, candidates: list[float]) -> float:
    feasible = [
        budget for budget in candidates if budget >= 0 and payment_rps + budget <= capacity_rps
    ]
    return max(feasible, default=0.0)
