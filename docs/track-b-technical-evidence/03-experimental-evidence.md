# 03: Experimental evidence (measured; nothing extrapolated)

Source: `results/processed/strategy-summary.json` / `.md` / `-runs.csv`, produced by
`python -m backend.aorts.experiments.analyze_strategy_comparison` from the raw JSONL files listed (with
SHA-256) in [04-result-files-and-hashes.md](04-result-files-and-hashes.md).

## Setup (identical for every strategy)

- **Stack:** the Docker Compose *experiment profile* (`docker-compose.experiments.yml`): every
  service is its own process, with real HTTP and timeouts, on one laptop (Docker Desktop, 8
  logical CPUs). Synthetic data only.
- **Strategies:** static polling (simplified simulation schedule, not a regulatory rule),
  exponential backoff with jitter, fixed budget (3 per tick), AIMD, and AORTS (Track B
  implementation, `backend/aorts/recovery/`).
- **Same safety layers for all strategies:** BudgetGuard, PreDispatchGate, admission control,
  primary-capacity reservation (25 %), status-check bulkhead (3 workers) and FinancialSafetyGate.
- **Same workload per seed:**
  - a fresh database for every run;
  - seeded traffic;
  - fault decisions keyed by payment, so the strategy cannot change which payments fail;
  - the same fault timeline and the same attacks.

  Results are paired by seed.
- **Scenarios:**
  - **S1**: Bank B degraded for 10 s, Bank C healthy.
  - **S2**: S1 plus an A3 status-check flood (80 req/s) and false high-capacity telemetry.
  - **S3**: *held out*, a combination never used during development: relapse, Bank C mildly
    slow, poisoning throughout, flood and heavier bursts.
- **Seeds:** 30 per scenario (1000–1029). Completed runs per strategy:
  - S1: 30 for every strategy;
  - S2: 30, except exponential backoff with 29;
  - S3: 30 for every strategy.

  6 failed runs were excluded and counted. Two of the gaps came from Docker engine hangs under
  host memory pressure; the affected seeds were re-run whole, and only completed (strategy, seed)
  pairs were used.
- **AORTS parameters:** the defaults were written before any evaluation run. **No tuning was
  done on these scenarios.**
- **Statistics:** mean with a percentile-bootstrap 95 % CI; AORTS versus the strongest baseline
  *per metric* (the baseline with the best mean); paired differences by seed.

## AORTS vs strongest baseline (difference = AORTS − baseline, 95 % bootstrap CI)

| Metric (better) | S1 | S2 | S3 (held-out) |
|---|---|---|---|
| Ambiguous resolution, mean s (lower) | +1.96 [1.71, 2.21] vs fixed-budget: **worse** | +1.51 [0.79, 2.12] vs fixed-budget: **worse** | +2.00 [1.59, 2.40] vs fixed-budget: **worse** |
| Resolution p95 s (lower) | +5.79 [5.15, 6.42]: **worse** | +4.43 [3.07, 5.62]: **worse** | +5.92 [5.00, 6.79]: **worse** |
| Recovery backlog area (lower) | +65.9 [55.6, 76.9]: **worse** | +44.9 [21.8, 65.5]: **worse** | +127.8 [97.3, 161.3]: **worse** |
| Status-check amplification (lower) | −0.012 [−0.100, 0.075]: no clear difference | −0.026 [−0.093, 0.040]: no clear difference | **−0.068 [−0.112, −0.020]: AORTS better** (about 5 %) |
| Primary goodput (higher) | −0.007 [−0.043, 0.030]: no clear difference | −0.046 [−0.63, 0.49]: no clear difference | −0.27 [−0.84, 0.08]: no clear difference |
| Primary p95 latency (lower) | +5.7 [−3.2, 15.9] ms: no clear difference | +335 [−1285, 2044] ms: no clear difference | +435 [−22, 1269] ms: no clear difference |
| Deadline compliance | 100 % for all | 100 % for all | 100 % for all |
| Escalations | 0 for all | 0 for all | 0 for all |
| Recovery time after fault (lower) | +0.24 [0.05, 0.46] s: **worse** | +0.43 [0.15, 0.76] s: **worse** | +4.00 [3.25, 4.76] s: **worse** |
| **Financial invariant violations** | **0 in all 449 completed runs (150 + 149 + 150), every strategy** | | |

**Conclusion, stated plainly:** in these scenarios the Track B AORTS controller **did not beat
the strongest baseline** on ambiguous-transaction delay; it was clearly slower. Its only
statistically clear advantage was a ~5 % reduction in status checks per ambiguous transaction in
the held-out scenario S3. It showed no primary-payment benefit, because the switch's reservation
and bulkhead already protect primary payments for *every* strategy.

## Why (from the recorded decisions)

- AORTS picks the *smallest* feasible budget that clears the backlog within a 10 s target. Its
  median budget on the degraded route during faults was **1 (S1), 1 (S2), 1 (S3)** checks per tick, against 3 for fixed-budget. The
  objective trades speed for fewer checks, and the saving in checks was small or absent.
- **Capacity estimation lags at regime changes.** The independent input is the switch's 30 s
  median latency, which is longer than the 10 s fault phase. Per-run median relative error:
  - S1: 14 of 30 runs were within 28–43 %, and about 12 runs were 5–17× too high (stale,
    pre-fault data);
  - mean of per-run medians: S1 4.95, S2 4.97, S3 1.26.

  Even the conservative lower bound exceeded the reference capacity in 45–53 % of samples. The
  capacity-estimation feature is therefore **not validated**.

## Security effects (measured, equal for every strategy)

| Measure | S2 | S3 |
|---|---|---|
| A3 flood requests sent → admitted (mean per run) | 800 → about 110–115 | 240 → about 77–79 |
| Flood containment time (mean) | 1.2–1.9 s | 1.0 s |
| FinancialSafetyGate rejections | 0 (no unsafe mutation was attempted) | 0 |
| Recovery checks refused by the primary reserve (mean per run) | 0 | 0.6–1.1 |

## Metrics not reported (invalid)

- `stale_jobs_dropped` came from a dispatcher process counter that was **not reset between runs**
  (it is cumulative), so it is not a per-run value. Duplicate-job *injection* counts (about 7–9 per
  run) were reset per run and are valid.

## Earlier experiments (see RESULTS.md)

Flooding E4/E7, poisoning E5, replay E6/E9/E10 and end-to-end E1/E2/E3/E8 are documented in
`RESULTS.md`. That file also records the harness errors found and corrected along the way.
