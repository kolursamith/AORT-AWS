"""Paired comparison with a percentile bootstrap 95% confidence interval.

For each seed, a strategy and a baseline ran on the same workload. We bootstrap the MEAN of the
per-seed differences (strategy - baseline). If the 95% CI excludes 0, the difference is reported as
statistically clear at that level; otherwise it is reported as "no clear difference". This is
deliberately conservative wording, not a hypothesis-test zoo.
"""

from __future__ import annotations

import random
import statistics


def bootstrap_mean_ci(
    values: list[float], *, iters: int = 10_000, seed: int = 12345, alpha: float = 0.05
):
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return None
    rng = random.Random(seed)
    n = len(vals)
    means = sorted(statistics.fmean(rng.choices(vals, k=n)) for _ in range(iters))
    lo = means[int((alpha / 2) * iters)]
    hi = means[min(iters - 1, int((1 - alpha / 2) * iters))]
    return lo, hi


def paired(a_by_seed: dict, b_by_seed: dict, *, lower_is_better: bool) -> dict:
    seeds = sorted(set(a_by_seed) & set(b_by_seed))
    diffs = [
        a_by_seed[s] - b_by_seed[s]
        for s in seeds
        if a_by_seed[s] is not None and b_by_seed[s] is not None
    ]
    if len(diffs) < 2:
        return {"pairs": len(diffs), "verdict": "insufficient paired runs"}
    ci = bootstrap_mean_ci(diffs)
    mean = statistics.fmean(diffs)
    if ci[0] > 0 or ci[1] < 0:
        better = (mean < 0) if lower_is_better else (mean > 0)
        verdict = (
            "AORTS better (95% CI excludes 0)" if better else "AORTS worse (95% CI excludes 0)"
        )
    else:
        verdict = "no clear difference (95% CI includes 0)"
    return {
        "pairs": len(diffs),
        "mean_diff": round(mean, 4),
        "ci95": [round(ci[0], 4), round(ci[1], 4)],
        "verdict": verdict,
    }
