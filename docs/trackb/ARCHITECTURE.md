# AORTS Track B — Architecture

> Research prototype for a **synthetic mock banking environment**. It does not connect to
> real banks, NPCI, UPI or customer accounts.

## 1. Components

```
            customer channel (CHANNEL key) / Locust / dashboard traffic generator
                                   │  HMAC-signed
                                   ▼
┌──────────────────────── Remitter bank service :8100 (banks A1, A2, A3) ────────────────────────┐
│ POST /payments → LedgerService.create_payment (atomic debit)                                     │
│              → signed POST switch /pay (as A1/A2/A3) → verify switch RESPONSE signature          │
│              → evidence? SUCCESS: settle+CLOSED │ FAILED: reverse │ none/timeout: AMBIGUOUS      │
│ /events/transaction-state (signed by SWITCH, replay-protected) · /reversal · /safety/eligibility │
└───────────────────────────────┬─────────────────────────────────────────────────────────────────┘
                                │                         ▲ status checks (as the owning bank)
                                ▼                         │
┌──────────────────── Payment switch :8110 ────────────────────┐   ┌───── Dispatcher (worker) ─────┐
│ admission: identity binding → global → bank → route → Tier 2 │   │ every tick:                    │
│ core worker pool (switch-wide) + per-destination pools       │◄──┤  escalate expired (sim. T+1)   │
│ switch_transactions = authoritative switch record            │   │  candidates + route state      │
│ /check-status: record → beneficiary GET/fence when PENDING   │   │  controller.decide (mock|HTTP) │
│ signs /pay and /check-status responses                       │   │  BudgetGuard.clamp             │
└──────────────┬──────────────────────────────┬────────────────┘   │  eligibility + version check   │
               ▼                              ▼                    │  → queue (local | SQS)         │
     Beneficiary B :8121              Beneficiary C :8122          └────────────────────────────────┘
     credit / status / fence           (independent faults)
               │
               ▼
┌────────────────────────── PostgreSQL (authoritative) ──────────────────────────┐
│ accounts · transactions · financial_operations · ledger_entries                 │
│ transaction_events · outbox_events · processed_events · audit_events (chained)  │
│ switch_transactions · runtime_config · telemetry_observations · twin_state      │
│ experiment_runs · bank_identities                                               │
└─────────────────────────────────────────────────────────────────────────────────┘
               ▲
┌──────────── Control / lab service :8130 ─────────────┐      ┌──── Streamlit dashboard :8501 ───┐
│ telemetry ingestion + TelemetryValidator → twin store │◄─────┤ KPIs, routes, security, quotas,   │
│ honest probe (switch measurements → telemetry)        │      │ decisions, experiments, audit,    │
│ outbox relay → local sink | EventBridge               │      │ demo controls (admin token only)  │
│ metrics read models · faults · defenses · attacks     │      └───────────────────────────────────┘
│ experiment registry → results/raw/*.json              │
│ controller adapter endpoints for Track A              │
└───────────────────────────────────────────────────────┘
```

Services share one codebase and one Docker image; each runs as its own process
(`uvicorn --factory …:build`). They share one PostgreSQL database, but each service
writes only its own tables. That is a simplification: real banks would each have their
own database (see §5).

## 2. Code map

| Concern | Module |
|---|---|
| Contracts (schema 1.0) | [backend/aorts/contracts/](backend/aorts/contracts/) |
| Ledger, state machine, invariants | [ledger/service.py](backend/aorts/banking/ledger/service.py), [ledger/invariants.py](backend/aorts/banking/ledger/invariants.py), [ledger/models.py](backend/aorts/banking/ledger/models.py) |
| Remitter / switch / beneficiary | [backend/aorts/banking/](backend/aorts/banking/) |
| Fault injection | [banking/faults/](backend/aorts/banking/faults/) |
| Dispatcher + queues | [banking/dispatcher/](backend/aorts/banking/dispatcher/) |
| Security | [backend/aorts/security/](backend/aorts/security/) |
| Outbox, publisher, consumer, EventBridge | [backend/aorts/events/](backend/aorts/events/) |
| Track A adapters, twin store, DynamoDB | [backend/aorts/adapters/](backend/aorts/adapters/) |
| Control / lab API | [backend/aorts/control/](backend/aorts/control/) |
| Dashboard | [backend/aorts/dashboard/](backend/aorts/dashboard/) |
| Experiments | [backend/aorts/experiments/](backend/aorts/experiments/) |
| AWS CDK | [cloud/cdk/](cloud/cdk/) |

## 3. Payment workflow and evidence

1. **Debit (one DB transaction):** lock the sender row, check funds, then insert the
   `transactions` row (DEBITED, v1), the `financial_operations` row
   (`<txn>:DEBIT`, unique), and two `ledger_entries` (sender DEBIT, `<bank>-SUSPENSE`
   CREDIT). The same transaction writes `transaction_events`, an outbox row and an
   audit row, then commits.
2. **Forward** the signed `/pay` to the switch. The switch records the transaction and
   applies the core and per-destination capacity limits. It forwards to the beneficiary
   and records the result. A credit timeout means `PENDING`.
3. **Interpret the answer.** Only a signature-verified HTTP 200 whose body names the
   same transaction and says SUCCESS or FAILED counts as **evidence**. Anything else
   (timeout, 5xx, lost response, PENDING, unsigned answer) leaves the transaction
   **AMBIGUOUS**. A timeout is never treated as failure.
4. **SUCCESS:** CONFIRMED_SUCCESS, then SETTLE (suspense → settlement account), then
   CLOSED. **FAILED:** CONFIRMED_FAILED, then REVERSAL (suspense → sender, referencing
   the DEBIT), then REVERSED. Each runs in a single DB transaction.
5. **Recovery:** the dispatcher sends rate-limited status checks. A check counts against
   the transaction's allowance only when the switch *admitted* it. Past the simulated
   deadline, or once the allowance is used up, the transaction goes to `HANDOFF_UDIR`
   with the funds still held: nothing is reversed blindly.

### Why fencing exists

Consider a beneficiary that is slow, or whose response is lost. If the switch answered
"FAILED" just because it had no record yet, the remitter would reverse the payment while
the beneficiary might still credit it later. That would duplicate money. So the switch
answers FAILED only in three cases:
- it never forwarded the payment;
- the beneficiary explicitly declined it;
- it has **fenced** the beneficiary.

Fencing means inserting a `FENCE` claim row in the beneficiary's claim slot
(`processed_events` primary key `(beneficiary:<bank>, txn_id)`). That slot is the same
one a credit must claim. Whichever of the credit or the fence commits first wins, and
PostgreSQL's unique index decides any race. A late credit therefore fails cleanly. A
status check that finds no switch record inserts a FAILED *tombstone* in the same way,
so a late `/pay` cannot be processed either.

### Bulkhead and reconciliation

- **Bulkhead.** With `AORTS_SWITCH_CHECK_WORKERS > 0`, status checks run in their own worker pool,
  separate from payment processing. A flood that gets past admission control (defenses off, or a
  misconfiguration) can then only slow other *status checks*; it cannot make the switch refuse
  payments. It is off (`0`) by default, so the earlier experiments stay reproducible. Enable it for
  real deployments. See `tests/integration/test_bulkhead.py`.
- **Reconciliation.** Escalated (`HANDOFF_UDIR`) transactions keep their funds held. The dispatcher
  tries to reconcile them every `AORTS_RECONCILIATION_INTERVAL_SECONDS` (10 s) through
  `/api/v1/reconciliation/status`, a separate low-rate switch channel that consumes no Tier 2
  credits. The same evidence rules apply: signed SUCCESS gives settlement, and signed FAILED (after
  fencing) gives an exactly-once reversal. Either way the state becomes
  `RESOLVED_BY_RECONCILIATION`.
- **Quarantine release** is a manual, admin-only decision: back to `AMBIGUOUS` or forward to
  `HANDOFF_UDIR`.

## 4. Status-check recovery loop and Track A

```
candidates (AMBIGUOUS, backoff elapsed, under allowance, before deadline)
   └─► RecoveryDecisionRequest{routes: measured + twin belief + integrity, candidates}
         └─► Controller (Track A via HTTP | MockController | fallback on error)
               └─► RecoveryDecision (recommendation only)
                     └─► BudgetGuard: min(recommended, hard cap, switch-measured headroom);
                                      integrity ≠ TRUSTED → min(fallback, last trusted budget)
                           └─► per txn: eligibility() → start_check(expected_version) → DispatchJob
                                 └─► worker: signed /check-status → apply_evidence(expected_version)
```

Policies are pluggable through `AORTS_CONTROLLER_MODE`: `mock`, `http` (Track A) or
`baseline:<name>`. At most one status check is in flight per transaction (the `CHECK_IN_FLIGHT`
state), whatever the policy. This caps how aggressive any policy can be, and it is a deliberate
safety property.

The controller never touches the database. Financial mutations happen only in
`LedgerService`. It re-reads and locks the row, re-validates the version and evidence
inside the committing transaction (TOCTOU-safe), and relies on database uniqueness
constraints as the last line of defence.

## 4b. Digital twins, AORTS recovery intelligence and safety gates

The full algorithm flow is in
[docs/track-b-technical-evidence/01-architecture-and-algorithm-flow.md](docs/track-b-technical-evidence/01-architecture-and-algorithm-flow.md).
In short:
- `backend/aorts/twin/` keeps Route and Transaction Twins, projected from recorded `outbox_events` and
  `telemetry_observations`. It is idempotent, ordered by version, and can be rebuilt from the
  event log.
- `backend/aorts/recovery/` estimates capacity with a confidence bound, evaluates candidate budgets
  while reserving primary capacity, and ranks transactions with an anti-starvation rule. It is
  exposed as `AORTS_CONTROLLER_MODE=aorts`.
- The **PreDispatchGate** (`backend/aorts/recovery/gates.py`) applies to every strategy.
- The ledger's **FinancialSafetyGate** (`backend/aorts/banking/ledger/safety_gate.py`) re-checks state,
  evidence and uniqueness immediately before every posting.
- The switch enforces a **primary-capacity reservation** (`AORTS_SWITCH_PRIMARY_RESERVE_FRACTION`,
  default 0.25).

## 5. Distributed-transaction limitations (deliberate)

- The remitter debit and the beneficiary credit are **separate local transactions**.
  There is no 2PC or consensus protocol. Consistency comes from idempotent operations,
  evidence rules, fencing and escalation, which is how real payment networks reconcile
  too.
- All services share one PostgreSQL instance for convenience. The code keeps their
  tables separate, and a production design would give each bank its own database.
- The outbox relay gives at-least-once delivery. SQS and EventBridge are also
  at-least-once, so every consumer is idempotent.
- The in-memory parts are single-replica: the nonce cache, the rate-limit buckets and
  the Tier 2 credits (one switch task). With several replicas, buckets and nonces would
  need a shared store (Redis or DynamoDB). Financial safety never depends on them.

## 6. Local vs AWS

| Concern | Local (Compose) | AWS (CDK) |
|---|---|---|
| Compute | containers on one host | ECS Fargate services + Cloud Map `*.aorts.local` |
| Database | `postgres:16-alpine`, bound to 127.0.0.1 | RDS PostgreSQL 16, private isolated subnets, encrypted |
| Secrets | `.env` (generated, git-ignored) | Secrets Manager (RDS-generated + app secret) |
| Dispatch queue | in-process `LocalQueue` | SQS + DLQ (maxReceiveCount 5) |
| Twin state | `twin_state` table (same CAS semantics) | DynamoDB conditional writes |
| Domain events | local sink | EventBridge → Lambda → S3 archive (add-on) |
| Metrics | in-process + dashboard | CloudWatch EMF + alarms |
| Results | `results/` | S3 bucket (encrypted, 90-day lifecycle) |
| Experiment campaign | scripts / dashboard | Step Functions → ECS RunTask (add-on) |
