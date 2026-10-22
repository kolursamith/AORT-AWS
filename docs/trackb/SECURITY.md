# AORTS Track B — Security design, assumptions and residual risks

> Teaching prototype in a synthetic mock environment. The controls below are meant to be
> meaningful and testable. They are **not** a complete enterprise or production security
> system, and no compliance claim is made.

## 1. Threat model (in scope)

| Threat | Example | Primary defenses |
|---|---|---|
| T1 Status-check flooding | Compromised bank A3 floods `/check-status` | Identity binding, global/bank/route token buckets, Tier 2 credits, switch core pool |
| T2 Telemetry poisoning | Probe reports low latency / high throughput | Source auth, schema, time window, sequencing, ranges, cross-check with switch measurements, EWMA, confidence, BudgetGuard fallback |
| T3 Transaction replay | Duplicate/stale responses, repeated reversals, concurrent races | HMAC + nonce + timestamp, event-id uniqueness, version checks, DB unique constraints, row locks |
| T4 Forged evidence | Unsigned or mis-linked "FAILED" answer | Response signatures, linkage checks, evidence rules |
| T5 Quota evasion | A3 claims to be A1 | Bank identity comes from the verified key, never from the body |
| T6 Lab misuse | Attack endpoints reachable in production | Endpoints disabled by default (404), separate admin token, host allow-list, never enabled in AWS |

Out of scope: a compromised PostgreSQL superuser, a compromised host or container
runtime, side channels, a stolen admin token used from inside the VPC, insider fraud,
and the security of Track A's own code.

## 2. Request authentication

- **HMAC-SHA256** over: `AORTS-HMAC-SHA256`, the method, the path, the query, the Unix
  timestamp, a nonce (16–128 chars) and `SHA256(body)`, separated by newlines. The
  headers are `X-AORTS-Key-Id`, `X-AORTS-Timestamp`, `X-AORTS-Nonce` and
  `X-AORTS-Signature`. See [request_auth.py](backend/aorts/security/request_auth.py).
- The signature is checked with `hmac.compare_digest` **before** the nonce store is
  touched, so unauthenticated traffic cannot burn nonces.
- The timestamp window is ±300 s and nonces are remembered for 600 s. A byte-identical
  replay fails with `AUTH_REPLAYED_NONCE`, an old one with `AUTH_EXPIRED_TIMESTAMP`, and
  a tampered body with `AUTH_INVALID_SIGNATURE`.
- Every key has a version (`A1-v1`, `A1-v2`). To rotate: add the new key to
  `AORTS_SERVICE_KEYS`, move `AORTS_SIGNING_KEYS` to it, wait out the old key's in-flight
  window, then remove the old key.
- **Roles** are separate: payment channel, remitter bank, switch, beneficiary,
  dispatcher, controller, telemetry source. Admin and lab actions use a *different*
  credential (`X-AORTS-Admin-Token`, constant-time compare), and the deployment flags
  `AORTS_ENABLE_FAULT_CONTROLS` / `AORTS_ENABLE_ATTACK_CONTROLS` gate them on top of that.
- **Response signing:** the switch signs its `/pay` and `/check-status` responses. The
  remitter only accepts evidence whose response signature verifies against the `SWITCH`
  identity (rule G).
- Logs carry at most a 10-character SHA-256 fingerprint of a signature. Secret-looking
  keys and DSN passwords are redacted by the JSON log formatter.
- Request bodies above 64 KiB are rejected. Pydantic models forbid unknown fields and
  use strict integer paise. Outbound calls have explicit timeouts.

**Limitations:** symmetric shared secrets (no PKI or mTLS). In local mode every service
gets the whole keyring from `.env`. The nonce cache is per process (see
ARCHITECTURE §5). Production would use mTLS or workload identity, a per-service key
distribution, and a shared nonce store.

## 3. Financial safety engine (rules A–J)

Implemented in [ledger/service.py](backend/aorts/banking/ledger/service.py) and
[ledger/invariants.py](backend/aorts/banking/ledger/invariants.py).

| Rule | How it is enforced | Test |
|---|---|---|
| A Idempotency | PK `operation_id`; unique `idempotency_key`; unique `(transaction_id, operation_type, bank_id)` | `test_reversal_exactly_once_and_balanced`, replay suite |
| B Valid transitions | explicit `TRANSITIONS` map; `expected_version` vs the locked row | `test_transition_map_rejects_illegal_moves`, `test_stale_version_rejected` |
| C Evidence | timeout/PENDING/NOT_FOUND/unsigned is not evidence; reversal requires CONFIRMED_FAILED | `test_unknown_outcome_does_not_permit_reversal`, `test_lost_response_makes_payment_ambiguous_not_failed` |
| D Balancing | every operation posts a 2-leg journal; `verify_ledger` checks debits = credits | `verify_ledger` in many tests |
| E Conservation | sender's net ledger effect matches the state (‑amount, or 0 if reversed); balance = opening + entries | `test_verify_ledger_detects_tampering` |
| F Causality | reversal `references_operation_id = <txn>:DEBIT` of the same transaction; op ids cannot be reused across transactions | `test_operation_id_cannot_be_reused_for_another_transaction` |
| G Source auth | evidence `source` = authenticated `SWITCH`; response signature verified | `test_unauthenticated_failure_evidence_rejected` |
| H Concurrency | `SELECT … FOR UPDATE`; unique constraints as backstop; **one global lock order**: transaction row first, then every touched account, sorted, *before* inserting or updating rows with FKs to them. This fixes two deadlocks found under load (see RESULTS corrections). | concurrent payment/event/reversal tests |
| I Uncertainty | preserve AMBIGUOUS; deadline/allowance → HANDOFF_UDIR; QUARANTINED blocks automation | `test_deadline_escalates_instead_of_inventing_outcome`, `test_quarantine_blocks_automated_evidence` |
| J Auditability | accepted operations are audited in the same DB transaction; rejected ones in a separate transaction after rollback | `test_audit_chain_verifies` |

A balanced ledger is **not** proof of correctness: a duplicated transfer still
balances. That is why uniqueness (A), causality (F) and evidence (C) are enforced
separately.

## 4. Security Module 1 — status-check flooding

The admission order at the switch (all checks are evaluated first, then all buckets are
debited together, so a rejected request consumes nothing):

1. **Identity binding:** the claimed `bank_id` must equal the HMAC identity, and the
   route must start with that bank. Otherwise `IDENTITY_MISMATCH` (403).
   Transaction ids carry the remitter bank (`TXN<bank>-<16 hex>`), and the switch also
   rejects (403, audited as `FOREIGN_TRANSACTION_ID`) a status check, reconciliation query
   or `/pay` whose id belongs to another bank. This happens *before* any lookup, so a
   bank cannot fence (write a "not received" tombstone for) or read another bank's
   transaction. A duplicate `/pay` answers only the bank that owns the record. Ids
   without a namespace (older data) keep the previous record-based check.
2. **Global ceiling** (default 60/s, burst 100) → `GLOBAL_CAPACITY_EXCEEDED`.
3. **Per-bank bucket** (10/s, burst 20) → `RATE_LIMITED`.
4. **Per-route bucket** (15/s, burst 30) → `ROUTE_RATE_LIMITED`.
5. **Tier 2 credits** (cap 60, refill 5/s per bank; re-allocatable by backlog) →
   `CREDIT_EXHAUSTED`.

All rejections are 429 with a `Retry-After` header. Only admitted checks occupy the
switch core pool or reach a beneficiary. A rejected check does not count against the
transaction's allowance (`check_not_admitted` returns it to AMBIGUOUS with
`checks_used` unchanged).

**Defense in depth:** the optional status-check bulkhead (`AORTS_SWITCH_CHECK_WORKERS`) keeps
payment processing capacity separate from status-check capacity, even if admission control is
bypassed or disabled. The reconciliation channel has its own small per-bank bucket and uses no
credits, so it cannot be used to get around flood limits.

**Tier 2 credits are this project's proposed mechanism, not a regulatory standard.** The
policy is documented in [tier2_credits.py](backend/aorts/security/tier2_credits.py). The rate
values are illustrative defaults to calibrate per machine, not universal safe limits.

## 5. Security Module 2 — telemetry poisoning

Each observation gets a verdict TRUSTED / SUSPICIOUS / REJECTED and a confidence score
([telemetry_validator.py](backend/aorts/security/telemetry_validator.py)).

- **Hard rejection:** unauthenticated source, signer ≠ `source_id`, an unregistered
  source, a schema version other than 1.0, a timestamp older than 30 s or more than 5 s
  in the future, out-of-range or NaN values, p95 < p50, no metrics at all, a duplicate
  `event_id`, or a sequence number not above the last one accepted.
- **Confidence penalties:**
  - missing values;
  - disagreement with the switch's **independent** measurements, penalised more heavily
    in the *optimistic* (unsafe) direction;
  - no independent measurement available at all (an uncorroborated sample is never
    trusted);
  - EWMA anomalies that the switch measurements do not corroborate;
  - **persistent optimistic bias** (one-sided CUSUM per source, route and metric): each
    sample adds its relative optimistic deviation from the switch measurement minus a 10 %
    allowance; at a score of 0.6 the metric is no longer treated as corroborated. A lie
    that stays inside the per-sample tolerance (e.g. +30 % throughput every sample)
    accumulates and is flagged within a few samples, while honest noise of a few percent
    does not. The score is capped, so an honest source recovers within a few samples.
    `AORTS_TELEMETRY_BIAS_CHECK` (default on).

  A sharp change that the switch measurements *do* corroborate is a real route change.
  It is learned rather than flagged, so a genuine Bank B slowdown does not freeze the
  twin.
- **Only TRUSTED observations** update the twin and the EWMA baselines.
- **BudgetGuard:** `applied = min(recommended, hard cap, switch-measured headroom)`. If
  integrity is not TRUSTED, `applied ≤ min(fallback, last trusted budget)`. Suspicious
  telemetry can therefore never *raise* the budget; it freezes or reduces it. A TRUSTED
  verdict older than `AORTS_TELEMETRY_MAX_AGE_SECONDS` (30 s) counts as **STALE**, so a
  probe that goes silent no longer keeps a route "trusted" indefinitely.

**Residual risk (measured, not hidden; see [SECURITY_VALIDATION.md](SECURITY_VALIDATION.md)):**
- a persistent optimistic bias **below the 10 % allowance** (+8 % in the experiments) is
  not detected;
- **pessimistic** lies (false-high timeout rate, inflated backlog) are not detected. They
  can only lower the budget, so the harm is slower recovery, never overload;
- when the measured value is below the metric's absolute floor (e.g. throughput under
  2 tps), the relative deviation is computed against the floor, so lies on a nearly idle
  route accumulate slowly.

The independent headroom cap bounds the budget in all of these cases. Statistical
detection never proves authenticity.

## 6. Security Module 3 — replay

These layers stack in order. See [replay_protection.py](backend/aorts/security/replay_protection.py).
1. HMAC timestamp + nonce.
2. Event freshness.
3. Linkage: the bank, route and amount must match the authoritative row.
4. `processed_events` primary key on `event_id`, in the same DB transaction as the effect.
5. `expected_version` checked against the locked row.
6. Financial-operation unique constraints.

The DynamoDB twin store uses conditional writes (`attribute_not_exists(pk) OR
version = :expected`). The local `twin_state` table implements the same semantics.
**Neither replaces the PostgreSQL atomicity used for money.**

## 7. Audit log

- Structured rows with an event id, UTC timestamp, experiment and correlation ids,
  actor, transaction, route, action, decision, reason, versions and metadata (redacted).
- Rows for financial changes are written **inside the same DB transaction**, so they
  cannot be lost after a commit.
- High-volume security decisions (floods) go through a buffered writer. It keeps exact
  in-memory counters and samples rows (5 per actor, reason and second), so the audit
  log does not become a DoS amplifier. Exact counts come from the switch metrics.
- **Hash chain:** `hash = SHA256(prev_hash ‖ canonical_json(row))`, appended under a
  PostgreSQL advisory lock. To verify it, call `GET /api/v1/audit/verify` (admin) or run
  `verify_chain()`. This is **tamper-evident, not tamper-proof**: an attacker with full
  database write access can rewrite every row and recompute the chain unless the head
  hash is periodically anchored somewhere independent, such as an S3 Object Lock bucket
  (not implemented).

## 8. AWS posture (CDK)

- ECS task roles are least-privilege:
  - DynamoDB access only for control and the dispatcher;
  - SQS access only for the dispatcher;
  - S3 put only for control and the experiment task;
  - no long-lived AWS keys anywhere.
- RDS sits in isolated subnets with no public endpoint. Its security group admits 5432
  only from the task security group. Storage is encrypted, and the credentials are
  generated into Secrets Manager.
- The app secret (service keys, admin token) also lives in Secrets Manager and is
  injected as ECS secrets.
- The ALB exposes only `/api/v1/payments*` and `/api/v1/health` of the remitter, and
  only to the `AllowedCidr` parameter (default `127.0.0.1/32`, i.e. nobody).
- Lab and fault controls are forced off (`false`) in every AWS service.
- S3: encrypted, public access blocked, SSL enforced. SQS: SSE, SSL enforced, DLQ.
  DynamoDB: encrypted.
- Log groups keep 1 week of logs. Alarms cover the DLQ, queue age, ALB 5xx and latency,
  RDS and service CPU, unhealthy remitter targets, status-check rejection floods and
  authentication failures.
- **Residual:** the ALB uses HTTP (there is no domain or ACM certificate in a student
  account). Add an HTTPS listener before sending anything but synthetic data. Traffic
  inside the VPC between services is plain HTTP with HMAC.

## 9. Responsible use

The attack simulators refuse targets outside `AORTS_ATTACK_ALLOWED_HOSTS` (localhost,
Compose names, the test server). They also cap rate and duration. Never point them, or
Locust, at any system you do not own and have not isolated for testing.
