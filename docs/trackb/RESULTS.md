# AORTS Track B — Measured results (local, synthetic)

> These are **measurements from one Windows 11 laptop** (8 logical CPUs, 8 GB RAM visible to Docker),
> taken on 2026-10-14/15. The environment is a **synthetic mock**. Absolute latencies say nothing
> about real payment systems; only the *comparisons between arms of the same experiment* are
> meaningful. Every number below can be traced to a file in `results/` produced by the scripts in
> `backend/aorts/experiments/`. Where fewer than five runs were done, or something went wrong, this page
> says so.

Notation: values are mean ± half-width of a 95% CI (t-distribution, n = 5 runs) unless stated.

## Harness corrections made during this work (read first)

1. **Flooding, first attempt: superseded.** The concurrent payment generator reused seeded
   idempotency keys across runs, so after the first run of each repeat the payments were
   idempotent *replays*. Its payment-latency columns were meaningless. The files are kept in
   `results/*/superseded/` with a NOTE. After the fix (a unique per-run key nonce plus a regression
   test), the experiment was re-run, and **0 replays** occurred in any run.
2. **End-to-end recovery time.** The first formula reported 0.1 s by catching a transient zero.
   The metric is now "first sample after fault removal from which the Bank B backlog stays 0"
   (stability window = rest of the run). Recomputing it from the saved samples gives **5.2 s**.
3. **Telemetry validator (a defect, found by the poisoning experiment and fixed before the final
   runs).** A genuine Bank B slowdown was being flagged as an anomaly and froze the twin, and
   uncorroborated optimistic telemetry could be TRUSTED. Corroborated changes are now learned,
   and uncorroborated samples are never trusted.

4. **Ledger deadlocks (a real bug, found by the policy-comparison load).** Concurrent payments from
   the same sender deadlocked: the FK `KEY SHARE` lock taken by the transaction insert conflicted
   with `FOR UPDATE` on the same account, and settlement had a separate lock-order cycle. A first
   constrained-flood run had 21 payments fail with HTTP 500 because of it. Fix: one global lock order
   (transaction row → all touched accounts, sorted, before any FK-bearing insert or update), plus two
   regression tests. All runs reported below were made after the fix. The affected run is in
   `superseded/`.
5. **Overlapping experiment runs (my operational error).** A stopped chain kept running its next
   steps, concurrently with the restarted chain, on the shared experiments database. Two outputs
   were invalidated and moved to `superseded/`. In-process experiments now hold a PostgreSQL
   advisory lock, so a second concurrent run is refused (verified).

## E1/E2/E3/E8 — End-to-end on the Docker Compose stack

Three runs (n = 3), real containers, dispatcher with the mock controller, `--phase 20 --rps 5`:
20 s normal → 20 s Bank B degraded (300 ms latency + 50 % lost responses) → 20 s restored → drain.
Run 1: `results/raw/end-to-end-20261014T181333.json`. Run 2 (after a confirmed reset):
`results/raw/end-to-end-20261014T184604.json`. Run 3 (after the deadlock fix, with the bulkhead enabled):
`results/raw/end-to-end-20261016T175354.json`.

| Measure | Run 1 | Run 2 | Run 3 |
|---|---|---|---|
| Payment requests sent (A1/A2 → B/C) | 301 | 308 | 307 |
| Customer retries with the same key (→ idempotent replay, no 2nd debit) | 1 | 8 | 7 |
| Distinct transactions → final state | 300 → **300 CLOSED / SUCCESS** | 300 → **300 CLOSED / SUCCESS** | 300 → **300 CLOSED / SUCCESS** |
| Escalated / reversed | 0 / 0 | 0 / 0 | 0 / 0 |
| Max ambiguous backlog on **Bank B** while degraded | 5 | 5 | 5 |
| Max ambiguous backlog on **Bank C** at the same time | **0** | **0** | **0** |
| Ambiguous transactions resolved by signed status checks | 22 | 28 | 26 |
| Status checks used (admitted / rejected / per ambiguous txn) | 22 / 0 / 1.0 | 28 / 0 / 1.0 | 26 / 0 / 1.0 |
| Resolution latency p50 / p95 | 1.83 s / 4.52 s | 2.04 s / 3.05 s | 2.056 s / 3.273 s |
| Bank B backlog stable at 0 after fault removed | 5.2 s (recomputed, see correction 2) | 3.2 s | 5.3 s |
| Ledger verification | **ok** | **ok** | **ok** |

No payment was declared failed because of a timeout. Every lost response was resolved as SUCCESS by
a signed status check, because the beneficiary had in fact credited it. Bank C was unaffected by
Bank B's degradation.

## E5 — Telemetry poisoning (model-in-the-loop **simulation**)

`results/processed/poisoning-20261014T181508.csv`, 5 seeds × 6 cases × 3 arms, 60 ticks each.
The attack starts at tick 10, and Bank B's capacity halves at tick 20.

**Simulation caveat:** a deterministic fluid-queue model supplies the ground truth and the
"switch measurements". The real Track B validator, twin update rule, mock controller and budget
guard make the decisions. The seeds only vary ±5 % measurement noise, so the 5 runs are *not*
independent stochastic trials. That is why most CIs are ~0, and they are omitted here.

| Poisoning case | Arm | Mean budget (checks/tick) | Max budget | Mean actual delay (ms) | Ticks > 1 s (of 50) | Flagged samples |
|---|---|---|---|---|---|---|
| (all cases) | clean | 3.76 | 10 | 362 | 0 | 0 (no false positives through the real slowdown) |
| throughput ×4 | unprotected | 30.0 | 30 (hard cap) | 19 255 | 40 | 0 |
| throughput ×4 | protected | 2.0 | 2 | 360 | 0 | 50 |
| latency ×0.2 | unprotected | 9.84 | 12 | 1 670 | 33.4 | 0 |
| latency ×0.2 | protected | 2.0 | 2 | 360 | 0 | 50 |
| timeout/backlog hidden | unprotected | 20.0 | 20 | 10 200 | 39 | 0 |
| timeout/backlog hidden | protected | 2.0 | 2 | 360 | 0 | 50 |
| replayed old telemetry | unprotected | 11.0 | 12 | 2 820 (sd 580) | 35.8 | 0 |
| replayed old telemetry | protected | 2.0 | 2 | 360 | 0 | 50 (all REJECTED) |
| invalid / missing values | unprotected | 9.94 | 12 | 1 757 (sd 62) | 33.8 | 0 |
| invalid / missing values | protected | 2.0 | 2 | 360 | 0 | 50 |
| **subtle throughput ×1.3** | unprotected | 13.94 | 22 | 4 370 | 39.2 | 0 |
| **subtle throughput ×1.3** | protected | 3.76 | 10 | 362 | 0 | **0 (not detected)** |

Reading:
- In every non-subtle case, the protected arm flagged the poisoning at the first attacked tick,
  never let the budget rise, and kept the delay at the clean level.
- The **cost** of protection: during an attack it falls back to 2 checks/tick, below the clean
  arm's 3.76, so recovery is slower while integrity is in doubt.
- **Limitation (at the time of this experiment):** the +30 % lie stays inside the cross-check
  tolerance and is *not detected*. Harm is avoided only because the budget is independently capped
  by switch-measured headroom.

  > **Update 2026-10-22.** A persistent-bias (CUSUM) check was added to the validator. In the
  > security validation package (5 seeds) the +30 % lie is now flagged in 95 % of attack samples in
  > this model and in 58 % on the live ingestion path, still with 0 false positives; a bias below the
  > 10 % allowance remains undetected. See [SECURITY_VALIDATION.md](SECURITY_VALIDATION.md). The
  > table above is the original experiment and is left unchanged.

The live ingestion path was also exercised through the control API (`test_poisoning_attack_via_lab`).
All poisoned samples (900 tps claim, impossible range, unauthenticated, spoofed source, duplicate
event id) came out non-TRUSTED.

## E6/E9/E10 — Replay, reversal race, invalid authorization

`results/processed/replay-20261014T181447.csv`: 5 repeats × 2 modes (auto-reverse on/off) =
**10 full runs × 21 cases**, each on a freshly reset database, with real signed HTTP through the
in-process stack.

| Case | Result in all 10 runs |
|---|---|
| 1 valid status response once | 200 ACCEPTED |
| 2 same response delivered again | 409 DUPLICATE_EVENT, no new posting |
| 3 stale version / 3b stale timestamp | 409 STALE_VERSION, state unchanged (debit only) |
| 4 same reversal delivered 3× | 1st ACCEPTED (or DUPLICATE if already auto-reversed), then DUPLICATE; **1 reversal posting** |
| 4b reversal with a new operation id | 409 ALREADY_APPLIED, still 1 posting |
| 5 same event, 10 concurrent deliveries | exactly 1 accepted |
| 6 8 concurrent reversals (mixed op ids) | **1 reversal posting** |
| 7 duplicate after close / 7b conflicting outcome after close | DUPLICATE no-op / 409 ILLEGAL_TRANSITION, outcome unchanged |
| 8 response for the wrong transaction / 8b signed by a non-switch identity | 409 WRONG_TRANSACTION / 403 AUTH_FORBIDDEN |
| 9 missing / invalid idempotency key | 422 VALIDATION_ERROR |
| 10a–e captured request reused, expired timestamp, tampered body, unsigned | 401 REPLAYED_NONCE / EXPIRED_TIMESTAMP / INVALID_SIGNATURE / MISSING |

Across all 210 case executions: **0 duplicate reversal postings**, and ledger verification was
**ok in 10/10 runs**.

## E4/E7 — Status-check flooding

Open-loop attack by A3 for 5 s at 10, 50 or 100 req/s. A1 and A2 send 3 legitimate checks/s each,
and continue for 2 s after the attack stops. Concurrent payments run at 4/s. Real signed HTTP through
the in-process stack. Defense limits: bank 10/s (burst 20), route 15/s, global 60/s, Tier 2 credits
on. Containment criterion: the first 1 s window from which A3's admitted checks stay ≤ 1.5 × its bank
limit. Every arm: n = 5, **0 idempotent replays**, 0 deadlocks, ledger verification ok.

Two capacity profiles were calibrated (as the brief asks):
- **default switch** (`results/processed/flooding-20261014T182546.csv`): 16 core workers × 5 ms, i.e.
  far more capacity than the attack rates;
- **constrained switch** (`results/processed/flooding-constrained-20261016T173606.csv`, re-run after
  the deadlock fix): 4 workers × 50 ms ≈ 80 requests/s with a shared status-check/payment pool, so the
  100 req/s stage exceeds capacity.

**A3 (attacker) admitted checks in 5 s** (both scenarios):

| Attack rate | Defenses off | Defenses on | Detection (first 429) | Containment |
|---|---|---|---|---|
| 10 req/s | 50 | 50 (within its quota, so nothing to stop) | — | 0 s |
| 50 req/s | 250 | **69** (= burst 20 + 10/s) | 0.48 s | 1 s |
| 100 req/s | 500 (default) / ≈450 (constrained; the rest overloaded) | **69** | 0.22 s | 1 s |

**Constrained switch, A3 → Bank B, mean ± 95 % CI half-width:**

| Scenario, attack rate | Arm | A1/A2 checks succeeded | A1/A2 check p95 during attack (ms) | Payment p95 (ms) | Payments refused by overloaded switch (reversed) |
|---|---|---|---|---|---|
| same route (A1/A2 also on B), 100/s | off | **91.4 % ± 4.5** | **927 ± 15** | **1 355 ± 124** | in 5/5 runs (1–4 of 28 each) |
| same route, 100/s | on | **100 %** | **89 ± 9** | **477 ± 130** | 0 |
| healthy route C ("mixed"), 100/s | off | 89.1 % ± 4.5 | 927 ± 16 | 1 176 ± 45 | in 4/5 runs (1–5 of 28 each) |
| healthy route C, 100/s | on | 100 % | 89 ± 13 | 341 ± 73 | 0 |
| same route, 50/s | off / on | 100 % / 100 % | 86 ± 6 / 80 ± 8 | 310 ± 80 / 400 ± 161 | 0 / 0 |
| same route, 10/s | off / on | 100 % / 100 % | 87 ± 28 / 73 ± 7 | 323 ± 54 / 374 ± 115 | 0 / 0 |

Reading:
- **Effectiveness:** with defenses on, A3 is cut to its quota (69 of 250–500) within 0.22–0.48 s, and
  rejected requests never reach the switch core or a beneficiary (also asserted by
  `test_flood_rejected_at_switch_before_downstream_work`).
- **Protecting legitimate users:** this only matters once the flood exceeds switch capacity. At
  100 req/s on the constrained switch the undefended flood:
  - makes A1/A2 status checks about **10× slower** (p95) and drops 9–11 % of them;
  - nearly **triples payment p95**, with non-overlapping CIs;
  - makes the switch refuse some payments outright (each one safely reversed: no money lost or
    duplicated).

  With the defense, A1/A2 stay near their baseline and every payment closes.
- At 10 and 50 req/s the switch still has headroom, so there is no measurable harm and no
  measurable defense overhead (payment CIs overlap).
- **Healthy route C is not isolated from a switch-wide overload** (the "mixed" rows look like the
  same-route rows), because status checks and payments share the switch core pool here. Admission
  control, or the bulkhead (next section), is what protects them. Bank C isolation from a degraded
  *Bank B* is shown in the end-to-end run (0 ambiguous on C).
- **Default profile:** A3 is restricted exactly the same way (69 vs 250/500). A1/A2 were served 100 %
  in every arm with p95 18–27 ms, i.e. no measurable defense overhead and no measurable harm at that
  capacity.
- Post-attack recovery is measured properly in the next section (E7, with a baseline lead and a 12 s
  tail).

## E7 — Post-attack recovery, and the status-check bulkhead

`results/processed/flooding-e7-recovery-20261016T171200.csv` (shared pool) and
`flooding-e7-bulkhead-20261016T171550.csv` (status checks in a separate 2-worker pool). Setup:
constrained switch (4 core workers × 50 ms), 3 s of legit-only baseline, 5 s A3 flood at 100 req/s
on route B, then 12 s of legit traffic. n = 5 per arm, 0 idempotent replays. **Recovery** = the
first 1 s window after the attack stops from which every legit check succeeds and has p95 ≤
max(2 × baseline, baseline + 50 ms). Its resolution is therefore 1 s.

| Switch | Defenses | Baseline legit p95 (median, ms) | Legit checks succeeded | Legit p95 during attack (ms) | Recovery after attack stops | Payments (4/s) |
|---|---|---|---|---|---|---|
| shared pool | off | 63 | 98.0 % ± 1.6 | 927 ± 17 | **3.0 s** (all 5 runs) | 1 of 80 refused by the overloaded switch (and safely reversed) in 4 of 5 runs |
| shared pool | on | 62 | **100 %** | 89 ± 12 | **0 s** | all 80 closed in every run |
| bulkhead (2 check workers) | off | 63 | 81.8 % ± 2.9 | 920 ± 19 | 3.0 s (all 5 runs) | **all 80 closed in every run** |
| bulkhead (2 check workers) | on | 64 | 100 % | 378 ± 20 | 2.0 s (0–3 s; unstable) | all closed |

Reading:
- Admission control is what protects legitimate *status checks*. With it on there is nothing to
  recover from (0 s).
- The **bulkhead protects payments** even when the flood is undefended (0 payments refused vs about
  1 per run). The price is fewer workers for status checks: legit checks get slower (p95 378 ms with
  defenses on) and fail more often when undefended (82 %).
- So the bulkhead is a capacity-sizing trade-off, not a free win. Compose and CDK use 8 check workers
  next to 16 core workers.

## Recovery-policy comparison (Weakness E)

`results/processed/policy-comparison-20261016T171950.csv`: 6 policies × 2 arms × 3 repeats = 36
runs, in-process. Scenario: 12 payments/s to Banks B and C. Bank B is degraded for 15 s (200 ms
latency, 60 % errors on credits *and* status queries, 40 % lost responses), then restored for 8 s,
then drained. Ledger backoff is set to 0, so the policy alone decides timing. Simulated T+1 deadline
is 30 s. "raw" = Track B budget guard off; "guarded" = on. Admission control is on in both. Ledger
verification was ok in all 36 runs.

| Policy | Arm | Ambiguous txns | Resolved before deadline | Resolution p50 / p95 (s) | Checks per ambiguous txn | 429 rejections | Payment goodput (/s, ≤ 2 s) | Recovery after fault (s) |
|---|---|---|---|---|---|---|---|---|
| naive-polling | raw | 65 ± 9 | 100 % | 1.51 ± 0.63 / 4.86 ± 1.59 | 2.48 ± 0.26 | 0 | 9.12 ± 0.63 | 1.30 ± 0.83 |
| naive-polling | guarded | 64 ± 2 | 100 % | 1.15 ± 0.42 / 3.58 ± 1.91 | 2.54 ± 0.51 | 0 | 9.22 ± 0.11 | 0.94 ± 0.79 |
| npci-static (sim.) | raw | 65 ± 7 | 100 % | 2.03 ± 1.94 / 4.98 ± 3.47 | 2.27 ± 0.52 | 0 | 8.70 ± 1.98 | 2.15 ± 1.43 |
| npci-static (sim.) | guarded | 66 ± 12 | 100 % | 1.86 ± 1.81 / 5.87 ± 6.31 | 2.37 ± 0.13 | 0 | 8.46 ± 2.01 | 3.95 ± 8.78 |
| exp-backoff-jitter | raw | 64 ± 5 | 100 % | 1.46 ± 1.96 / 5.77 ± 4.63 | 2.42 ± 0.29 | 0 | 8.88 ± 1.15 | 1.81 ± 2.15 |
| exp-backoff-jitter | guarded | 67 ± 6 | 100 % | 1.02 ± 0.14 / 4.40 ± 2.23 | 2.38 ± 0.30 | 0 | 9.07 ± 0.27 | 1.33 ± 0.57 |
| fixed-budget (3) | raw | 64 ± 2 | 100 % | 1.33 ± 0.36 / 4.52 ± 3.02 | 2.63 ± 0.40 | 0 | 9.22 ± 0.11 | 1.46 ± 0.80 |
| fixed-budget (3) | guarded | 68 ± 9 | 100 % | 1.43 ± 0.64 / 4.52 ± 0.21 | 2.33 ± 0.30 | 0 | 9.00 ± 0.47 | 1.81 ± 1.88 |
| aimd | raw | 68 ± 5 | 100 % | 2.66 ± 0.81 / 5.91 ± 2.47 | 2.25 ± 0.03 | 0 | 9.04 ± 0.21 | 2.69 ± 2.11 |
| aimd | guarded | 66 ± 13 | 100 % | 2.74 ± 2.71 / 4.98 ± 2.65 | 2.17 ± 0.14 | 0 | 8.52 ± 0.99 | 3.41 ± 2.75 |
| mock (stand-in) | raw | 64 ± 1 | 100 % | 3.72 ± 1.30 / 6.47 ± 0.11 | 2.04 ± 0.16 | 0 | 9.20 ± 0.06 | 3.86 ± 1.50 |
| mock (stand-in) | guarded | 68 ± 7 | 100 % | 3.47 ± 1.35 / 5.87 ± 0.54 | 1.95 ± 0.14 | 0 | 9.03 ± 0.25 | 4.00 ± 1.47 |

Reading (honestly a **null result** for this workload):
- At n = 3 no policy is clearly better. Every policy resolved everything before the deadline with no
  escalations, no rate-limit rejections and no switch overload, and most CIs overlap.
- The only consistent tendency is the usual trade-off: the mock controller (oldest-first,
  capacity-proportional) uses the fewest checks per transaction but resolves slowest, while
  naive / backoff / fixed are faster and use slightly more checks.
- Why naive polling did not cause the expected overload feedback: Track B allows **at most one
  status check in flight per transaction** and admission control sits in front of the switch, so
  polling pressure is bounded by design. Showing the feedback-collapse effect that AORTS targets
  needs a harsher setting (more ambiguous transactions than check capacity, real network timeouts).
  That is Track A's evaluation. Track B provides the harness (`--policies http` adds the AORTS
  controller).

## Strategy comparison: AORTS vs baselines (30 seeds × 3 scenarios, Docker)

Full detail and CIs: [docs/track-b-technical-evidence/03-experimental-evidence.md](docs/track-b-technical-evidence/03-experimental-evidence.md);
data: `results/processed/strategy-summary.*`.

- **Ambiguous-transaction delay:** AORTS was **slower than the strongest baseline (fixed budget)
  in all three scenarios.** Mean resolution was 3.1 / 4.5 / 4.6 s against 1.2 / 3.0 / 2.6 s; every
  95 % CI excludes 0.
- **Status checks per ambiguous transaction:** about 5 % lower than AIMD in the held-out S3
  (CI excludes 0); no clear difference in S1 or S2.
- **Primary payments:** no clear difference in goodput, p95 latency or success ratio. The switch
  reservation and bulkhead protect primary payments for every strategy.
- **Deadline compliance:** 100 % for every strategy. Escalations: 0. **Financial invariant
  violations: 0 in all 449 completed runs.**
- **Capacity estimation:** not validated. It lags at regime changes, with per-run median errors
  of 5–17× in roughly 40 % of S1 runs.

The earlier "Recovery-policy comparison" section above was an in-process null result. This Docker
campaign supersedes it as the main strategy comparison.

## Not measured / not done

- **AWS:** the stack was synthesized but not deployed, so there are no cloud measurements.
- Track A's own controller (`intelligence/` in the team repository) has not been compared. Only
  Track B's AORTS implementation, the baselines and the mock were run.
- Database RTO/RPO were not measured. Nothing here claims an RPO improvement.
- T+1 deadlines are simulated in real seconds (`AORTS_DEADLINE_SECONDS`), not calendar days.
