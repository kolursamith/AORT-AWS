"""Paired bootstrap comparison used for AORTS vs strongest baseline."""

from backend.aorts.experiments.analyze_strategy_comparison import analyze
from backend.aorts.experiments.paired_stats import bootstrap_mean_ci, paired


def test_clear_improvement_detected_and_null_not_overclaimed():
    a = {s: 1.0 + 0.01 * s for s in range(30)}
    b = {s: 2.0 + 0.01 * s for s in range(30)}
    assert paired(a, b, lower_is_better=True)["verdict"].startswith("AORTS better")
    noisy = {s: (1.0 if s % 2 else -1.0) for s in range(30)}
    zero = {s: 0.0 for s in range(30)}
    assert paired(noisy, zero, lower_is_better=True)["verdict"].startswith("no clear difference")
    assert paired({1: 1.0}, {1: 2.0}, lower_is_better=True)["verdict"] == "insufficient paired runs"


def test_bootstrap_ci_contains_mean():
    lo, hi = bootstrap_mean_ci([1, 2, 3, 4, 5])
    assert lo <= 3 <= hi


def test_strongest_baseline_is_best_per_metric():
    rows = []
    for seed in range(5):
        for strat, val in (("aorts", 3.0), ("aimd", 2.0), ("fixed-budget", 4.0)):
            rows.append(
                {
                    "scenario": "X",
                    "strategy": strat,
                    "seed": seed,
                    "resolution_mean_s": val + 0.1 * seed,
                    "primary_goodput_per_s": 5.0,
                }
            )
    c = analyze(rows)["X"]["aorts_vs_strongest_baseline"]["resolution_mean_s"]
    assert c["strongest_baseline"] == "aimd" and c["verdict"].startswith("AORTS worse")
