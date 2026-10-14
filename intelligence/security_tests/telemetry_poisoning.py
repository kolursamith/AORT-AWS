"""Produce test-only outliers without sending them to a network target."""


def inject(sample: dict, *, latency_factor: float = 100.0) -> dict:
    poisoned = dict(sample)
    poisoned["p95_latency_ms"] = float(sample["p95_latency_ms"]) * latency_factor
    poisoned["test_only_poisoned"] = True
    return poisoned
