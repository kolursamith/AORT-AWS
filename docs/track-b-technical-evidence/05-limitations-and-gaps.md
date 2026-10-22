# 05: Limitations and unresolved gaps

## Model and algorithm

- **Capacity model is simple.** The point estimate is `workers / median latency` of one
  destination pool. It ignores the switch-wide core pool and the status-check pool, and it
  assumes that latency reflects service time. Under heavy queueing the median latency includes
  waiting, so the estimate is conservative. That direction is safe, but it costs recovery speed.
- **Latency prediction is M/M/1-style** (`p50 / (1 − ρ)`). It was chosen to be explainable, not
  exact. Predicted-versus-observed values are recorded so the error is visible, not assumed away.
- **Fixed weights and thresholds.** Scheduler weights, `rho_max`, margins and confidence
  thresholds are hand-set defaults. They were written before the evaluation runs and **not
  tuned** on them. They are not optimised either, so other settings could do better or worse.
- **One status check in flight per transaction** (a Track B safety property) bounds how
  aggressive *any* strategy can be. Differences between strategies are therefore smaller than in
  a system without that property.
- **Primary-payment protection comes mainly from structure** (the switch reservation, the
  bulkhead and admission control), which applies to *all* strategies. The experiments therefore
  cannot attribute primary-payment protection to AORTS specifically.
- **No ML.** The optional ML estimator was not implemented.

- **Measured outcome of the comparison (see 03):** AORTS resolved ambiguous transactions
  **slower** than the strongest baseline in all three scenarios. It reduced status checks by about
  5 % in the held-out scenario only, and showed no primary-payment benefit. Its "smallest feasible
  budget meeting a 10 s clearance target" objective is the main cause: the median budget was 1
  check per tick.
- **Capacity estimation lags at regime changes.** The switch's 30 s latency window outlasts a 10 s
  fault, so many runs estimate from stale, pre-fault data, with errors of 5–17×. Its accuracy is
  not validated.
- **Candidate follow-ups (not done, so the held-out evaluation stays clean):** a shorter or
  event-driven measurement window; a clearance target derived from deadlines and backlog instead
  of a fixed 10 s; then a fresh held-out evaluation.

## Twins

- Switch measurements are snapshots, not events, so twin reconstruction covers the
  event-derived fields only: transaction lifecycle, primary load, backlog and telemetry
  reliability. Measured latency and capacity are refreshed from the next measurement.
- Under reordering, the per-transaction *counters* (checks started, inconclusive checks) are
  best-effort: an older CHECK event that arrives after a newer state is stale and not counted.
  The *state*, *version* and *outcome* always converge.

## Experiments

- **Synthetic mock environment on one laptop** (Docker Desktop, 8 logical CPUs; the host had
  under 2 GB of RAM free at times). Absolute latencies say nothing about real systems.
- Workload equivalence across strategies relies on keyed fault decisions (beneficiary account and
  amount). Two payments with the same account *and* amount share a fault decision, which is
  rare and harmless to the comparison. Thread timing remains unavoidable noise; seed pairing
  mitigates it.
- "Ground truth" capacity for the estimation-error metric is `workers / (fault latency + base
  service time)`. The base time is calibrated per run from 5 no-fault payments. It is an
  approximation, not an oracle.
- The campaign runner health-checks the stack before every run and aborts after 3 consecutive
  failures. Failed runs are counted and reported, never silently dropped.
- Experiments record a **source-tree SHA-256** instead of a Git commit. Runs were made from the
  local working folder, which is not a Git repository. The code was later committed to
  `kolursamith/AORT-AWS` (`feature/backend`).
- The raw and processed result files are committed under
  `infra/trackb/evidence/strategy-campaign/` (a Track B-owned folder; `results/` belongs to the
  other track). All 60 files listed in 04 match their recorded hashes; the folder's README
  reconciles 452 result lines with 449 unique runs (3 duplicates of a re-run seed).
- The dispatcher counter `stale_jobs_dropped` was cumulative across runs and is not reported.
- The Docker engine hung twice under host memory pressure (under 2–3 GB free). Affected runs were
  excluded, and the seeds were re-run whole.

## Security and engineering

- HMAC with shared symmetric keys (no PKI or mTLS). In local mode every service holds the full
  keyring.
- Nonce cache, token buckets and Tier 2 credits are in-memory and single-replica.
- The audit hash chain is tamper-evident, not tamper-proof (no external anchoring).
- All banks share one PostgreSQL instance (with logically separate tables), and there is no
  cross-service 2PC.
- **AWS was not deployed**: the CDK stack only synthesizes.
- A first ledger version had two deadlocks under concurrency. They were found by load and fixed
  with a global lock order, and regression tests now cover them. This is noted because it shows
  that the concurrency tests did not initially cover same-sender concurrency.
