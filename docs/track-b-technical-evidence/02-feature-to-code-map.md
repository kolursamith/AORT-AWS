# 02: Technical feature → code → verification map

Status legend: **I&T** Implemented and Tested · **I/NT** Implemented but Not Tested · **Partial** ·
**Proposed** (idea only) · **Not Impl.**

| # | Technical feature | Module(s) | Verified by | Status |
|---|---|---|---|---|
| F1 | Transaction Twin: status, evidence, retries, version, deadline, maintained from recorded events | `backend/aorts/twin/transaction_twin.py`, `backend/aorts/twin/engine.py` | `tests/unit/test_twins.py`, `test_aorts_integration.py::test_twin_reconstruction...` | I&T |
| F2 | Duplicate, stale, delayed, out-of-order and conflicting event handling by version (last-writer-wins on full state) | same | `test_duplicates_are_ignored`, `test_stale_and_delayed...`, `test_out_of_order_converges...` (20 shuffles), `test_conflicting_same_version...` | I&T |
| F3 | Twin reconstruction from the recorded event log, equal to the incremental state | `TwinEngine.rebuild()` | `test_twin_reconstruction_from_recorded_events_matches_incremental` | I&T (event-derived fields only; see 05) |
| F4 | Route Twin: capacity inputs, primary load from debit events, recovery backlog, latency, telemetry reliability (EWMA of verdicts) | `backend/aorts/twin/route_twin.py` | `test_route_twin_telemetry_reliability_and_backlog`, dashboard `/api/v1/twins` test | I&T |
| F5 | Confidence-aware capacity estimate from independent switch measurements; telemetry can never raise it | `backend/aorts/recovery/capacity.py` | `test_false_high_reported_capacity_does_not_raise_estimate`, acceptance **P1** (safety property) | **Partial**: the *cannot-be-raised-by-telemetry* property is I&T; estimation **accuracy is not validated** (lags at regime changes; see 03) |
| F6 | Recovery-budget optimizer: candidate ladder, primary reservation, utilisation and latency feasibility, smallest feasible budget meeting the target, reasons recorded | `backend/aorts/recovery/budget.py` | `test_optimizer_*`, `test_no_spare_capacity...`, 30-seed comparison (03) | I&T (behaves as specified; it did **not** reduce delays versus the baselines; see 03) |
| F7 | Conservative fallback when telemetry reliability or confidence is low | `budget.py`, `security/budget_guard.py` | `test_unreliable_telemetry_forces_conservative_fallback`, acceptance **P5** | I&T |
| F8 | Priority scheduler (urgency, age, evidence, retries) with an anti-starvation force rule and per-transaction explanation | `backend/aorts/recovery/scheduler.py` | `test_scheduler_*`, `test_starvation_guard...` | I&T |
| F9 | Pre-dispatch safety gate (route budget, in-flight limit, primary-capacity reservation, eligibility), applied to every strategy | `backend/aorts/recovery/gates.py`, `banking/dispatcher/service.py` | `tests/unit/test_gates_and_faults.py` (every branch), `gate_refusals` in decision records | I&T |
| F10 | Execution-time financial safety gate: authoritative re-read, evidence check, uniqueness re-check before every posting; a timeout is never evidence | `backend/aorts/banking/ledger/safety_gate.py`, `ledger/service.py::_post` | `test_gate_refuses_*`, `test_every_committed_operation_carries_gate_facts`, the replay suite | I&T |
| F11 | Switch-enforced primary-capacity reservation (recovery may not occupy the reserved share of the destination / shared core pool) | `banking/switch/capacity.py`, `switch/service.py` | acceptance **P4** (with a mutation check: fails when the reservation is off), `test_primary_reserve_protects_payments...` | I&T |
| F12 | Status-check bulkhead (separate worker pool) | `switch/service.py` | `tests/integration/test_bulkhead.py` | I&T |
| F13 | Admission control against status-check flooding: identity binding, global/bank/route buckets, Tier 2 credits | `security/defense_policy.py`, `tier2_credits.py` | `tests/test_rate_limits.py`, acceptance **P3** | I&T |
| F14 | Telemetry integrity validation (authentication, schema, time window, sequence, ranges, independent cross-check, corroborated-change learning) | `security/telemetry_validator.py` | `tests/test_telemetry_integrity.py` | I&T |
| F15 | Replay protection (HMAC nonce and timestamp, event-id uniqueness, version checks, DB constraints) | `security/request_auth.py`, `replay_protection.py`, ledger | `tests/test_replay_protection.py`, acceptance **P2** | I&T |
| F16 | Deterministic, keyed fault injection, so workloads are equivalent across strategies | `banking/faults/fault_service.py` | `test_keyed_fault_decisions_are_deterministic_and_order_independent`, campaign (identical ambiguous counts per seed) | I&T |
| F17 | Runtime strategy switching with a fresh controller per run | dispatcher `_maybe_switch_controller`, `POST /api/v1/admin/controller` | `test_controller_switch_endpoint_validates_mode`, experiment campaign | I&T |
| F18 | Baseline strategies (static schedule, exponential backoff with jitter, fixed budget, AIMD, naive polling) | `adapters/baseline_policies.py` | `tests/unit/test_baseline_policies.py` | I&T |
| F19 | Paired, seed-matched comparison with bootstrap 95% CIs against the strongest baseline per metric | `experiments/paired_stats.py`, `analyze_strategy_comparison.py` | `tests/unit/test_paired_stats.py` | I&T |
| F20 | ML-based capacity or outcome estimation | (none) | (none) | **Not Impl.** (optional in the requirements; deliberately left out) |
| F21 | Cross-replica shared nonce store and shared rate-limit state | (none) | (none) | **Proposed** |
| F22 | External anchoring of the audit hash chain (e.g. S3 Object Lock) | (none) | (none) | **Proposed** |
| F23 | Team-contract (`contracts/v1`) surface and shared-contract controller adapter: timeouts map to `unknown`, evidence-only `authoritative`, strict validation of controller decisions with fallback | `backend/aorts/adapters/team_routes.py`, `team_contract.py`, `controller_client.py` | `tests/trackb/integration/test_team_contract.py` | I&T (against a contract-only stub) |
