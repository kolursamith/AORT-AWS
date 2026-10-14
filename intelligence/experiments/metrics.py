"""Research metrics with explicit definitions."""

from statistics import median


def summarize_resolution_ms(samples: list[float]) -> dict[str, float]:
    if not samples:
        return {"median_ms": 0.0, "p95_ms": 0.0}
    ordered = sorted(samples)
    p95_index = min(len(ordered) - 1, int(0.95 * len(ordered)))
    return {"median_ms": float(median(ordered)), "p95_ms": float(ordered[p95_index])}
