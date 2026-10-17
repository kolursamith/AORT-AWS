# 01: Architecture and algorithm flow (as implemented)

## Components on the recovery path

```
 payments ──► remitter ──► switch ──► beneficiary B / C            (mock banks, signed HTTP)
                 │            │
                 ▼            ▼
          PostgreSQL ledger (authoritative)  ◄── execution-time FinancialSafetyGate
                 │  outbox_events, telemetry_observations   (recorded, append-only)
                 ▼
          TwinEngine ──► Transaction Twins + Route Twins     (projections; never authoritative)
                 │
                 ▼
          AORTS controller: CapacityEstimator → BudgetOptimizer → PriorityScheduler
                 │  RecoveryDecision (budgets, ranked txns, reasons, predictions)
                 ▼
          Dispatcher: BudgetGuard → PreDispatchGate → start_check(version) → queue
                 │
                 ▼
          switch /check-status: identity binding → global/bank/route buckets → Tier 2 credits
                 → status-check bulkhead → primary-capacity reservation → beneficiary query/fence
                 │
                 ▼
          ledger.apply_evidence (row lock, version, evidence rules) → FinancialSafetyGate → posting
```

## Per-tick recovery algorithm (`backend/aorts/recovery/controller.py`)

1. **Refresh the twins.** `TwinEngine.poll()` reads new rows from `outbox_events` and
   `telemetry_observations` by id cursor. For each event:
   - a duplicate `event_id` is ignored;
   - a lower `transaction_version` is stale and ignored;
   - the same version with a different id is a conflict and is ignored;
   - a version gap is applied and counted.

   The latest switch measurements are attached to each Route Twin as snapshots.
2. **Estimate capacity per route** (`capacity.py`).
   - Point estimate = `workers / median latency`, from **independent** switch measurements.
     Reported telemetry is not a capacity input.
   - `confidence = sample_factor × (0.5 + 0.5 × telemetry_reliability) × stability`.
     Telemetry reliability is an EWMA of validator verdicts.
   - Lower bound = `point × (1 − 0.8 × (1 − confidence))`.
3. **Choose the recovery budget per route** (`budget.py`).
   - Reserve `primary_load × 1.25`. The primary load comes from the debit events, not from
     telemetry.
   - Evaluate the candidate ladder 0, 1, 2, 3, 5, 8, 13, 21, 34, 55. For each candidate:
     - predicted utilisation `ρ = (primary + B/tick) / capacity_low`;
     - predicted primary latency `p50 / (1 − ρ)`;
     - predicted clearance `backlog / (B/tick × p_resolve)`, where `p_resolve` is learned from
       check outcomes.
   - A candidate is feasible iff `reserved + B/tick ≤ 0.75 × capacity_low` and the predicted
     latency is ≤ 3 × p50.
   - Pick the **smallest feasible** budget that meets the clearance target (half the time left
     to the most urgent deadline, at most 10 s). If none meets it, pick the largest feasible
     budget. If none is feasible, pick 0, except a trickle of 1 when a deadline is imminent and
     ρ(1) < 1.
   - If confidence < 0.35 or reliability < 0.5, cap the result at the conservative fallback
     (2 per tick).
   - Record every candidate and the reasons.
4. **Rank the transactions** (`scheduler.py`).
   - `score = 0.40·urgency + 0.25·age + 0.20·evidence + 0.15·retry`.
   - Any transaction idle for at least `starvation_s` (8 s) is forced to the front.
   - Every score is recorded with its components.
5. **Make the safety decisions independently of the recommendation.**
   - The BudgetGuard enforces the hard cap, switch-measured headroom and the telemetry-integrity
     fallback.
   - The PreDispatchGate enforces the per-tick route budget, the in-flight limit per route, the
     primary-capacity reservation from switch measurements, and ledger eligibility (state,
     version, allowance).
   - The ledger moves the transaction to `CHECK_IN_FLIGHT` with a version check before the job
     is queued.
6. **Execute** (switch). Admission control, then the status-check bulkhead, then a
   destination-pool slot. Recovery callers may occupy at most `workers − ceil(0.25 × workers)`
   of that pool.
7. **Mutate money only at the ledger.** The transaction row is locked, the accounts are locked
   in sorted order, and the version and evidence are checked. Then the **FinancialSafetyGate**
   re-reads the authoritative row and re-checks:
   - evidence: authenticated SUCCESS or FAILED (a timeout is never evidence);
   - operation uniqueness.

   Only then is the balanced journal posted. The DB unique constraints remain the last line of
   defence.
8. **Explain.** The decision, with estimates, candidates, selection, reasons and
   predicted-versus-observed values, goes to the audit log and to the twin store key
   `aorts:snapshot`, which the dashboard reads.

## Twin reconstruction

`TwinEngine.rebuild()` replays the recorded event tables from id 0.
`test_twin_reconstruction_from_recorded_events_matches_incremental` checks that the rebuilt state
fingerprint equals the incrementally maintained one after real interleaved activity.
`test_out_of_order_converges_to_same_state` covers 20 random shuffles of a lifecycle.

Switch measurements are snapshots, not events, so they are not part of reconstruction (see 05).
