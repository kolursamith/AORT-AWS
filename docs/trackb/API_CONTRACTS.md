# AORTS Track B — API contracts (schema version 1.0)

> Synthetic mock environment. Simplified UPI-*like* semantics; **not** ISO 8583,
> ISO 20022, UPI or IMPS compliant.

The source of truth is the Pydantic code in [backend/aorts/contracts/](backend/aorts/contracts/).
Example payloads live in [backend/aorts/contracts/fixtures/](backend/aorts/contracts/fixtures/), and
[tests/contract/](tests/contract/) checks that every fixture still validates.

## 1. Conventions

| Rule | Detail |
|---|---|
| Timestamps | ISO-8601 with a UTC offset (`2026-10-14T10:00:15Z`). A timestamp without a timezone is rejected. |
| Money | `amount_paise`: **integer** paise, `0 < amount ≤ 1_000_000_000`. Floats are rejected (`StrictInt`). |
| IDs | `^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,79}$`. Banks: `A1`, `A2`, `A3` (remitters), `B`, `C` (beneficiaries). Routes: `<remitter>-<beneficiary>`, e.g. `A1-B`. |
| Transaction ids | Issued by the remitter as `TXN<remitter bank>-<16 hex>`, e.g. `TXNA1-3F2A9C0B7D1E5A44`. The switch answers status checks, reconciliation queries and `/pay` only for ids in the signer's own namespace (otherwise 403 `IDENTITY_MISMATCH`). Ids without a bank namespace (older data, test fixtures) are still accepted and checked against the stored record's owner. |
| Enums | Explicit string values; see [enums.py](backend/aorts/contracts/enums.py). New values can be added, but existing ones are never renamed. |
| Unknown fields | Rejected (`extra="forbid"`). |
| Errors | Every non-2xx response has this shape: `{"error_code", "message", "retryable", "details", "schema_version"}`. |
| Versioning | `schema_version: "1.0"`. A breaking change means a new version and a new fixture set. |

### Error codes

`VALIDATION_ERROR`, `AUTH_MISSING`, `AUTH_INVALID_SIGNATURE`, `AUTH_UNKNOWN_KEY`,
`AUTH_EXPIRED_TIMESTAMP`, `AUTH_REPLAYED_NONCE`, `AUTH_FORBIDDEN`, `IDENTITY_MISMATCH`,
`RATE_LIMITED`, `ROUTE_RATE_LIMITED`, `GLOBAL_CAPACITY_EXCEEDED`, `CREDIT_EXHAUSTED`,
`SWITCH_OVERLOADED`, `DEPENDENCY_UNAVAILABLE`, `INVALID_ROUTE`, `INSUFFICIENT_FUNDS`,
`ACCOUNT_NOT_FOUND`, `TRANSACTION_NOT_FOUND`, `IDEMPOTENCY_CONFLICT`, `DUPLICATE_EVENT`,
`STALE_VERSION`, `ILLEGAL_TRANSITION`, `INSUFFICIENT_EVIDENCE`, `WRONG_TRANSACTION`,
`ALREADY_APPLIED`, `FEATURE_DISABLED`, `TELEMETRY_REJECTED`, `INTERNAL_ERROR`.

HTTP status mapping: 401 (authentication), 403 (forbidden or identity mismatch),
404 (not found, or a lab endpoint that is disabled), 409 (state, version, duplicate or
idempotency conflict), 413 (body too large), 422 (validation or business rejection),
429 (rate limit or credits; carries a `Retry-After` header), 503/504 (overload or
dependency failure; retryable).

## 2. Authentication

Service calls carry four HMAC-SHA256 headers: `X-AORTS-Key-Id`, `X-AORTS-Timestamp`,
`X-AORTS-Nonce` and `X-AORTS-Signature`. The signing algorithm is described in
[SECURITY.md](SECURITY.md#2-request-authentication). The switch also signs its answers to
`/pay` and `/check-status` with the headers `X-AORTS-Response-Key-Id`,
`X-AORTS-Response-Timestamp` and `X-AORTS-Response-Signature`. The remitter only treats
a switch answer as evidence when that response signature verifies.

Administrative and lab endpoints use a separate `X-AORTS-Admin-Token`. Two deployment
flags also gate them:
- `AORTS_ENABLE_FAULT_CONTROLS` controls the fault, defense, traffic and reset endpoints.
- `AORTS_ENABLE_ATTACK_CONTROLS` controls the attack endpoints.

When a flag is off, its endpoints return 404.

| Identity | Role | May call |
|---|---|---|
| `A1`, `A2`, `A3` | `remitter_bank` | switch `/pay`, `/check-status` |
| `SWITCH` | `switch` | beneficiary `credit`/`fence`/`status`; remitter `/events/transaction-state` |
| `CHANNEL` | `payment_channel` | remitter `POST /payments`, reads |
| `DISPATCHER` | `dispatcher` | reads, reversal, eligibility, controller observations |
| `CONTROLLER` | `controller` | eligibility, reversal, decisions dry-run, Tier 2 allocation |
| `twin-probe-1/2` | `telemetry_source` | control `POST /telemetry/route` |

## 3. Remitter bank (port 8100)

| Method & path | Auth | Purpose |
|---|---|---|
| `POST /api/v1/payments` | CHANNEL | Atomic debit, forward to switch, interpret the answer. Body: `PaymentRequest`. A repeated `idempotency_key` returns the same transaction with `replayed: true`; the same key with a different body returns 409 `IDEMPOTENCY_CONFLICT`. Lab only: header `X-AORTS-Simulate: hold \| timeout \| lost-response`, which also requires the admin token and the fault flag. |
| `GET /api/v1/payments/{id}` | readers | `TransactionView` (versioned) |
| `GET /api/v1/payments/{id}/history?limit=&after_id=` | readers | Paginated `transaction_events` |
| `POST /api/v1/payments/{id}/simulate-timeout` | admin + fault flag | A held `DEBITED` payment becomes AMBIGUOUS without reaching the switch |
| `POST /api/v1/payments/{id}/simulate-lost-response` | admin + fault flag | Forwards a held payment, then discards the switch's answer |
| `POST /api/v1/payments/{id}/reversal` | controller / dispatcher | `ReversalRequest{operation_id, expected_version, reason}`. Exactly-once. |
| `POST /api/v1/payments/{id}/reconcile` | dispatcher / controller | For `HANDOFF_UDIR` only. Queries the switch reconciliation channel and applies authenticated evidence, giving `RESOLVED_BY_RECONCILIATION` (settled, or reversed exactly once). Returns `{resolved, reason \| result}`. |
| `POST /api/v1/payments/{id}/release-quarantine` | admin | `{reason, expected_version?, escalate?}`: `QUARANTINED` → `AMBIGUOUS` (automation resumes) or → `HANDOFF_UDIR` |
| `POST /api/v1/events/transaction-state` | SWITCH | Signed `TransactionStateEvent` (`event_type: STATUS_RESPONSE`, `payload.status`). Replay-protected. |
| `POST /api/v1/safety/eligibility` | controller / dispatcher | `EligibilityRequest{transaction_id, proposed_action, expected_version}`. Read-only safety check. |
| `GET /api/v1/health`, `GET /api/v1/metrics` | none | |

`PaymentRequest`:
```json
{"sender_account_id": "A1-CUST-0001", "beneficiary_account_id": "B-CUST-0001",
 "beneficiary_bank_id": "B", "amount_paise": 100000, "currency": "INR",
 "idempotency_key": "client-generated-8-to-80-chars", "correlation_id": "exp-001"}
```

`TransactionView` fields: `transaction_id`, `sender_account_id`,
`beneficiary_account_id`, `remitter_bank_id`, `beneficiary_bank_id`, `route_id`,
`amount_paise`, `currency`, `state`, `financial_outcome`, `workflow_state`, `version`,
`idempotency_key`, `checks_used`, `created_at`, `updated_at`, `deadline_at`,
`next_eligible_at`, `last_check_at`, `correlation_id`.

## 4. Payment switch (port 8110)

| Method & path | Auth | Purpose |
|---|---|---|
| `POST /api/v1/pay` | remitter bank | `SwitchPayRequest`. Idempotent on `transaction_id`. Signed `SwitchPayResponse{status: SUCCESS\|FAILED\|PENDING}`. |
| `POST /api/v1/check-status` | remitter bank | `StatusCheckRequest{transaction_id, bank_id, route_id}`. The admission order is identity binding → global ceiling → bank bucket → route bucket → Tier 2 credits. Answers with a signed `StatusCheckResponse`, or 429/403. |
| `POST /api/v1/reconciliation/status` | remitter bank | Same body and signed answer as `/check-status`, but it is a separate low-rate channel: per-bank bucket `AORTS_RECONCILIATION_RATE_PER_SEC` (1/s, burst 5) and no Tier 2 credits. Meant for escalated transactions. |
| `GET /api/v1/health` | none | |
| `GET /api/v1/metrics` | none | Counters, capacity pools (core, status-check bulkhead, destinations), per-destination measurements, defenses |
| `GET /api/v1/metrics/routes?window_s=` | none | Independent switch-side route measurements (latency p50/p95, throughput, timeout rate, backlog, workers) |
| `GET /api/v1/metrics/timeseries?seconds=` | none | Per-second counter buckets |
| `GET /api/v1/routes` | none | Route list and capacity |
| `GET /api/v1/security/banks/{bank_id}/quota` | none | `QuotaView`: bucket tokens, Tier 2 credits, defenses |
| `POST /api/v1/admin/security/reset` | admin | Refills buckets and credits and clears counters (between experiments) |
| `POST /api/v1/admin/tier2/allocate` | controller | `{total_checks_per_sec, backlog_by_bank}`. Proportional allocation with a floor. |

Status semantics: the switch answers `FAILED` only when the beneficiary did not, and can
never later, credit the payment. That holds in three cases: the switch never forwarded
the payment, the beneficiary declined it, or the beneficiary has been **fenced**.
Otherwise the answer is `PENDING`.

## 5. Beneficiary banks B (8121), C (8122)

| Method & path | Auth | Purpose |
|---|---|---|
| `POST /api/v1/beneficiary/credit` | SWITCH | `BeneficiaryCreditRequest`. `operation_id` must be `<txn>:CREDIT`. Credits at most once. |
| `GET /api/v1/beneficiary/transactions/{id}` | SWITCH | `SUCCESS \| FAILED \| NOT_FOUND` (read-only) |
| `POST /api/v1/beneficiary/transactions/{id}/fence` | SWITCH | Makes "not credited" permanent and returns the final status |

## 6. Control / ops / lab (port 8130) and the Track A controller adapter

| Method & path | Auth | Purpose |
|---|---|---|
| `GET /api/v1/metrics/summary` | none | Dashboard KPIs (measured): payments, resolution latency, `kpis{deadline_compliance, payment_goodput_per_s_60s, outcomes_per_status_check, switch_timeout_rate}`, security counters, quotas, ledger verification, twin state |
| `GET /api/v1/metrics/timeseries?seconds=` | none | Per-second payments created / ambiguous / resolved, plus the switch series |
| `GET /api/v1/decisions?limit=` | none | Recovery decisions: recommended vs guard-applied budgets |
| `GET /api/v1/audit?limit=&before_seq=&action=&transaction_id=` | admin | Paginated audit trail |
| `GET /api/v1/audit/verify` | admin | Recomputes the hash chain |
| `GET /api/v1/security/events?limit=&before_seq=` | none | Security-relevant audit rows |
| `POST /api/v1/telemetry/route` | telemetry_source | `RouteTelemetry` → validator verdict |
| `POST /api/v1/lab/telemetry/inject?as_source=` | admin + attack flag | **Test-only** ingestion |
| `GET /api/v1/telemetry/recent`, `GET /api/v1/twin/routes` | none | |
| `GET /api/v1/twins?max_txns=` | none | Route and Transaction Twin state (rebuilt from recorded events) plus the AORTS controller's last decision snapshot and dispatcher gate stats |
| `GET /api/v1/experiments/strategy-summary` | none | The analysed strategy comparison (`results/processed/strategy-summary.json`) |
| `POST /api/v1/admin/controller` | admin + fault flag | `{mode, run_id, seed}`: switch the dispatcher's strategy at runtime (a fresh controller per `run_id`). Modes: `mock`, `aorts`, `http`, `baseline:<name>` |
| `GET /api/v1/controller/health` | none | Mode and reachability of the controller adapter |
| `POST /api/v1/controller/decisions` | controller / dispatcher | Dry run: controller recommendation plus the budget-guard result. Nothing is dispatched. |
| `POST /api/v1/controller/observations` | dispatcher / controller | `DispatchResultObservation` forwarded to the controller |
| `POST /api/v1/admin/faults`, `/admin/faults/reset`, `GET /admin/faults` | admin + fault flag | `FaultConfig{target: switch\|remitter\|beneficiary:B\|route:B, latency_ms, error_rate, drop_response_rate, unavailable, capacity_workers}` |
| `POST /api/v1/admin/defenses` | admin + fault flag | `DefenseConfig{rate_limiting, tier2_credits, telemetry_validation, replay_protection}` |
| `POST /api/v1/admin/traffic/start` / `stop` | admin + fault flag | Synthetic payment load |
| `POST /api/v1/security/attacks/{flooding\|poisoning\|replay}/run` | admin + attack flag | `AttackRunRequest`. Returns an `experiment_id`. |
| `GET /api/v1/admin/experiments`, `/admin/experiments/{id}` | admin | Experiment metadata and results |
| `GET /api/v1/admin/export?fmt=json\|csv` | admin | Export (JSON is also saved under `results/processed/`) |
| `POST /api/v1/admin/reset` | admin + fault flag | Body `{"confirm":"RESET-SYNTHETIC-DATA"}`. Saves a snapshot first. Refused when `environment=aws`. |

### External controller integration (shared contract `contracts/v1`)

Controller modes: `mock` (the default stand-in), `aorts` (Track B's AORTS adaptive controller,
`backend/aorts/recovery/`), `http` (Track A), and `baseline:<name>`, where
`<name>` is `naive-polling`, `npci-static`, `exp-backoff-jitter`, `fixed-budget` or `aimd`. The
baselines are reference policies for comparisons; see
[baseline_policies.py](backend/aorts/adapters/baseline_policies.py).

When `AORTS_CONTROLLER_MODE=http`, Track B's outbound adapter
([controller_client.py](backend/aorts/adapters/controller_client.py) `HttpController`) speaks the
**shared team contract** (`docs/API_CONTRACTS.md`, `contracts/v1/models.py`) at
`AORTS_CONTROLLER_BASE_URL`:

| Track B calls | Body | Expected response |
|---|---|---|
| `GET /health` | none | any JSON, HTTP 200 |
| `POST /v1/recovery/decisions` (one call per route with ambiguous work) | `{experiment_id, route_id, estimated_capacity_rps, confidence, pending_transaction_ids}` | `contracts.v1.RecoveryDecision` |

* `estimated_capacity_rps` is Track B's **own** measurement of the switch (workers / p50), not
  the reported telemetry. `confidence` is the telemetry-integrity confidence when the verdict is
  TRUSTED, and 0 otherwise.
* `pending_transaction_ids` lists the route's ambiguous candidates, oldest first.
* The response is validated strictly against the shared model. A decision for another route,
  an already-expired decision (`expires_at`), or a selection containing IDs that Track B did not
  offer is rejected, and the fallback is used.
* `budget_rps` becomes checks per dispatcher tick: `min(len(selected), budget_rps × tick_s)`.
  `selected_transaction_ids` sets the order. The contract's predictions are recorded under
  `predicted[route]` in the decision log.
* The shared contract has no observation endpoint, so dispatch observations stay inside Track B.
* Tested against a stub that is built only from the shared contract
  (`tests/trackb/integration/test_team_contract.py`). No other owner's code is imported or run.

Track B always applies `min(recommended, hard cap, switch-measured headroom)`. When
telemetry integrity is not TRUSTED, the budget is also limited to
`min(fallback, last trusted budget)`. Before each dispatch Track B re-checks
eligibility and the transaction version. If the controller errors, times out or returns
something invalid, Track B uses a fixed fallback budget per route
(`AORTS_FALLBACK_BUDGET_PER_ROUTE`) and records `fallback_reason`.

## 7. Events

`DomainEvent` (outbox → local sink or EventBridge `DetailType=event_type`), for example
[ambiguous_transaction.json](backend/aorts/contracts/fixtures/ambiguous_transaction.json):
```json
{"event_id": "evt-1001", "schema_version": "1.0", "timestamp": "2026-10-14T10:00:15Z",
 "transaction_id": "TXNA1-3F2A9C0B7D1E5A44", "route_id": "A1-B", "bank_id": "A1", "amount_paise": 100000,
 "event_type": "PAYMENT_AMBIGUOUS", "transaction_version": 2, "correlation_id": "exp-001",
 "payload": {"last_known_state": "AMBIGUOUS", "checks_used": 0}}
```
Event types: `PAYMENT_DEBITED`, `PAYMENT_SUCCEEDED`, `PAYMENT_FAILED`,
`PAYMENT_AMBIGUOUS`, `CHECK_STARTED`, `CHECK_RESULT`, `REVERSAL_COMPLETED`,
`PAYMENT_CLOSED`, `ESCALATED`, `QUARANTINED`, `STATUS_RESPONSE`.

Delivery is **at-least-once**. Consumers must be idempotent; see
[consumer.py](backend/aorts/events/consumer.py).

`DispatchJob` (SQS body): `{job_id, schema_version, created_at, transaction_id,
route_id, bank_id, expected_version, decision_id, correlation_id}`. A redelivered job
fails the version check and is dropped.

Other schemas: `SecurityAlert`, `AuditRecord`, `ExperimentMetadata`
([events.py](backend/aorts/contracts/events.py)); `RouteTelemetry`, `FaultConfig`,
`DefenseConfig`, `AttackRunRequest` ([requests.py](backend/aorts/contracts/requests.py)).

## 8. Transaction state machine

```
DEBITED            -> CONFIRMED_SUCCESS | CONFIRMED_FAILED | AMBIGUOUS
AMBIGUOUS          -> CHECK_IN_FLIGHT | CONFIRMED_SUCCESS | CONFIRMED_FAILED | HANDOFF_UDIR | QUARANTINED
CHECK_IN_FLIGHT    -> CONFIRMED_SUCCESS | CONFIRMED_FAILED | AMBIGUOUS | HANDOFF_UDIR
CONFIRMED_FAILED   -> REVERSAL_INITIATED -> REVERSED
CONFIRMED_SUCCESS  -> CLOSED
HANDOFF_UDIR       -> RESOLVED_BY_RECONCILIATION
QUARANTINED        -> AMBIGUOUS | HANDOFF_UDIR
```
Each state maps to a `(workflow_state, financial_outcome)` pair; see `PROJECTION` in
[invariants.py](backend/aorts/banking/ledger/invariants.py). QUARANTINED and HANDOFF_UDIR keep
`financial_outcome = UNKNOWN`.

## 9. Team-contract surface (`contracts/v1`) served by Track B

Track B's services also serve the shared contract's HTTP surface
([team_routes.py](backend/aorts/adapters/team_routes.py), mappings in
[team_contract.py](backend/aorts/adapters/team_contract.py)). These routes are thin adapters over
the real ledger, admission control and evidence rules, and they keep Track B's HMAC
service-identity auth (section 2).

| Service | Method & path | Auth | Contract message |
|---|---|---|---|
| remitter | `POST /v1/payments` | payment_channel | body `{bank_id, route_id, amount_minor, currency: "INR", idempotency_key, experiment_id?, sender_account_id?, beneficiary_account_id?}` → `TransactionEvent` (202) |
| remitter | `GET /v1/payments/{id}` | channel / dispatcher / controller | `TransactionEvent` |
| switch | `POST /v1/status-checks` | remitter_bank; header `x-bank-id` must equal the signer | `{transaction_id, route_id, check_id}` → `StatusCheckResult` (quota and credit gated; 429 when limited) |
| beneficiary | `GET /v1/mock/status/{id}?route_id=` | switch | `StatusCheckResult` |
| control | `GET /v1/events?after_id=&limit=` | none | `[TransactionEvent]` from the transactional outbox; header `X-Next-After-Id` |
| control | `POST /v1/telemetry` | telemetry_source | `RouteTelemetry` → Track B validator verdict |

Mapping rules (financial meaning is preserved, never invented):
* `financial_outcome`: SUCCESS → `credited`; FAILURE or REVERSED → `not_credited`; anything else → `unknown`.
* A timeout is `unknown` / `ambiguous`, **never** `not_credited`.
* A `StatusCheckResult` is `authoritative` only for a SUCCESS or FAILED answer that has an
  evidence source (`SWITCH` or `beneficiary-<bank>`). PENDING and NOT_FOUND give `unknown`,
  `authoritative=false` and no evidence source.
* Track B differs from the minimal shared contract in one place: `POST /v1/payments` **requires**
  an `idempotency_key`, so a retried request can never debit twice.
