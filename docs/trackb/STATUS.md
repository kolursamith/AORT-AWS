# Track B: completion status

Scope: Track B only (mock banking, switch, ledger, security, twins, recovery intelligence,
experiments, dashboard, infra). Track A (`intelligence/`, `results/`) is not touched.

Status labels follow the project's reporting rules:
**Implemented and Tested**, **Implemented but Not Tested**, **Partial**, **Blocked**, **Not Implemented**.
"Tested" means automated tests in `tests/trackb/` run against a real PostgreSQL instance
(`AORTS_REQUIRE_DB=1`, so these tests fail instead of skipping when the database is missing).

## Core system

| Requirement | Status | Evidence |
|---|---|---|
| Remitter banks A1/A2/A3, switch, beneficiaries B/C (FastAPI services) | Implemented and Tested | `backend/aorts/banking/`, `tests/trackb/integration/test_payment_flow.py` |
| PostgreSQL ledger: atomic postings, unique operation IDs, state machine, version checks, fencing, global lock order | Implemented and Tested | `banking/ledger/`, `tests/trackb/test_ledger_invariants.py`; deadlock regressions in `integration/test_payment_flow.py` |
| Timeouts stay unknown/ambiguous, never failed; evidence-only resolution | Implemented and Tested | `test_payment_flow.py`, `test_recovery.py`, `test_team_contract.py` |
| Reconciliation and UDIR-style escalation at the deadline | Implemented and Tested | `test_reconciliation.py` |
| Transactional outbox and domain events | Implemented and Tested | `backend/aorts/events/`, `test_control_plane.py` |
| HMAC request signing, nonce and timestamp replay protection, signed switch responses | Implemented and Tested | `security/`, `tests/trackb/security/`, `test_replay_protection.py` |
| Flooding defense: identity binding, global/bank/route token buckets, Tier 2 credits | Implemented and Tested | `test_rate_limits.py` |
| Switch capacity protection: bulkhead and primary-capacity reservation | Implemented and Tested | `test_bulkhead.py` |
| Telemetry-poisoning defense: validator, cross-check against switch measurements, BudgetGuard | Implemented and Tested | `test_telemetry_integrity.py` |
| Protected security acceptance tests (not weakened to pass) | Implemented and Tested | `tests/trackb/acceptance/test_protected_security.py` |
| Audit hash chain | Implemented and Tested | `test_control_plane.py` |

## Additional requirements

| Requirement | Status | Evidence / note |
|---|---|---|
| Route Twin and Transaction Twin (event-sourced, version-ordered, rebuildable) | Implemented and Tested | `backend/aorts/twin/`, `tests/trackb/unit/test_twins.py` |
| AORTS recovery intelligence: capacity estimator, budget optimizer, priority scheduler | Implemented and Tested | `backend/aorts/recovery/`, `unit/test_recovery_intelligence.py`, `integration/test_aorts_integration.py` |
| Pre-dispatch gate and execution-time financial safety gate | Implemented and Tested | `unit/test_gates_and_faults.py`, `test_ledger_invariants.py` |
| Baselines: static polling, exponential backoff with jitter, fixed budget, AIMD | Implemented and Tested | `adapters/baseline_policies.py`, `unit/test_baseline_policies.py` |
| Same seeded workloads for every strategy (keyed deterministic faults) | Implemented and Tested | `workloads/scenarios.py`, `unit/test_traffic_generator.py` |
| Extended workloads S1 / S2 (flood + poison) / S3 (held-out relapse) | Implemented and Tested | `workloads/scenarios.py`; used in the campaign |
| Reproducible experiments: seed, scenario, config, source hash, run ID, raw results | Implemented and Tested | `experiments/provenance.py`, `docker_runner.py`. A source-tree SHA-256 is recorded instead of a Git commit, because the runs were made before the code was in Git. |
| 30 seeds per scenario with paired bootstrap 95% CIs vs the strongest baseline | Implemented and Tested | `experiments/paired_stats.py`, `unit/test_paired_stats.py`; 449 completed runs (6 failed, reported and excluded). See `RESULTS.md`. |
| Dashboard: twins, decisions, security, strategy comparison | Implemented but Not Tested | `backend/aorts/dashboard/app.py`; checked by hand, no automated UI tests |
| Patent-oriented technical evidence folder | Implemented | `docs/track-b-technical-evidence/` (documentation, not code) |
| Optional ML estimator | Not Implemented | It was optional. The capacity model is analytical (see evidence 05). |

## Team integration (shared `contracts/v1`)

| Requirement | Status | Evidence / note |
|---|---|---|
| Track B services serve the team-contract `/v1` surface (payments, status checks, mock status, events, telemetry) | Implemented and Tested | `adapters/team_routes.py`, `adapters/team_contract.py`, `integration/test_team_contract.py`; responses are validated with `contracts.v1.models` |
| Outbound controller adapter (`CONTROLLER_MODE=http`) speaks `POST /v1/recovery/decisions` | Implemented and Tested | `adapters/controller_client.py`. It is tested against a stub built only from the shared contract. Expired decisions and decisions with unknown IDs fall back. |
| End-to-end run against the real Track A controller | Not tested | Out of Track B's scope by instruction. The adapter is ready, and the integration is left to the joint integration step. |
| CI runs the Track B DB tests (PostgreSQL service in `ci.yml`) | Implemented but Not Tested in CI | Added in this change. It runs on the next PR to `developer`. |

## Infrastructure

| Requirement | Status | Note |
|---|---|---|
| Docker Compose stack and experiment profile | Implemented and Tested | `infra/trackb/docker-compose*.yml`; used for the 449-run campaign |
| AWS CDK stack | Partial | `infra/trackb/cdk/` synthesizes only |
| AWS deployment | Blocked | It needs AWS credentials and your explicit approval. No resources were created, and Free Tier coverage is not assumed. |

## Measured results (summary; details in `RESULTS.md` and evidence 03)

- AORTS resolved ambiguous transactions **slower** than the strongest baseline (fixed budget) in
  all three scenarios.
- It used about 5% fewer status checks in held-out S3 only.
- There was no primary-payment difference.
- There were 0 ledger invariant violations across all runs.
- The capacity estimate lags at regime changes, and its accuracy is not validated.

These are reported as measured. They are not tuned after the fact.
