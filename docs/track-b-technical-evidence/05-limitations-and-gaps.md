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
- During development the Docker engine hung once under load (host memory pressure) and had to be
  restarted. The campaign runner health-checks the stack before every run and aborts after 3
  consecutive failures. Failed runs are counted and reported, never silently dropped.
- Experiments record a **source-tree SHA-256** instead of a Git commit, because the project
  folder is not a Git repository.

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
