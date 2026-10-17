"""Metric definitions used in RESULTS.md."""

from backend.aorts.experiments.common import stats
from backend.aorts.experiments.run_end_to_end import stable_zero_time


def test_recovery_time_requires_backlog_to_stay_at_zero():
    s = [
        {"t": t, "ambiguous_B": b}
        for t, b in [(0, 3), (1, 0), (2, 4), (3, 1), (4, 0), (5, 0), (6, 0)]
    ]
    assert stable_zero_time(s, 1) == 4  # the transient zero at t=1 does not count
    assert stable_zero_time(s[:4], 0) is None


def test_stats_ci_uses_t_distribution_for_small_n():
    r = stats([10.0, 12.0, 14.0, 16.0, 18.0])
    assert r["n"] == 5 and r["mean"] == 14.0
    lo, hi = r["ci95"]
    assert round(hi - 14.0, 2) == round(2.776 * r["std"] / 5**0.5, 2)
    assert stats([])["mean"] is None


def test_flood_recovery_metric_uses_baseline_and_stability():
    from backend.aorts.security.flooding_simulator import FloodResult, RequestRecord

    cfg = {"attacker": "A3", "lead_s": 2.0, "duration_s": 2.0, "containment_factor": 1.5}
    recs = [
        RequestRecord("A1", "legit", t, 20.0, 200, None) for t in (0.2, 0.7, 1.2, 1.7)
    ]  # baseline 20 ms
    recs += [
        RequestRecord("A1", "legit", 4.3, 900.0, 200, None),  # still slow 0.3 s after attack end
        RequestRecord("A1", "legit", 5.4, 25.0, 200, None),
        RequestRecord("A1", "legit", 6.4, 22.0, 200, None),
    ]
    recs += [RequestRecord("A3", "attack", 2.1, 5.0, 429, "RATE_LIMITED")]
    s = FloodResult(cfg, recs).summary(bank_limit=10)
    assert s["baseline_legit_ms"] == 20.0
    assert s["recovery_after_attack_s"] == 1.0
    assert abs(s["attack_detection_s"] - 0.1) < 1e-9
