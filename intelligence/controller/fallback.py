"""Conservative decision policy used when models or telemetry are unavailable."""


def safe_budget(estimated_capacity_rps: float, confidence: float) -> float:
    if estimated_capacity_rps < 0 or not 0 <= confidence <= 1:
        raise ValueError("capacity must be nonnegative and confidence within [0, 1]")
    return round(estimated_capacity_rps * min(confidence, 0.25), 3)
