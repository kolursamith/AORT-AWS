# Stage 09 / 09R cross-track integration (local integration branch)

Branch `integration/stage09-trackb` (local only, not pushed), worktree
`CAD_project/AORT-aws-integration`. Base: Track A `feature/intelligence` at `5024e6c` (Stage 09
controller). Merged: Track B `origin/feature/backend` at `9e736d9`. Merge base `cb89ae3`.
Stage 09 (`89c8e07`) merged the two tracks and added the v1.1 client. Stage 09R (this commit)
completes the real loop: Track B ledger → v1.1 producers → Track A controller → Track B
execution gate → signed status check → result back into the controller.

## Stage 09 merge result (unchanged)

- Textual conflicts: none. The two tracks changed disjoint paths.
- Stage 09 ran with no database, so 109 Track B PostgreSQL tests were skipped. Stage 09R runs all
  of them against a real PostgreSQL 16 instance (see "Verification").

## Stage 09R changes

| file | change |
|---|---|
| `backend/aorts/banking/ledger/migrations/versions/0002_v11_provenance.py` | **new, additive** migration (see below) |
| `backend/aorts/banking/ledger/models.py` | ORM for the new columns/tables |
| `backend/aorts/banking/ledger/service.py` | records provenance at source: `experiment_id`, `check_id` (`chk-…`, set by `start_check`, cleared on leaving `CHECK_IN_FLIGHT`), `check_fact` on check outcomes, `evidence_id` on settle/reverse/reconcile; the debit's history row and outbox row now share one event id (bug fix) |
| `backend/aorts/banking/remitter/service.py` | every signed switch response is persisted as an `EvidenceRecord` exactly as received (key id, timestamp, signature, body digest, path, status) with the `verify_response` result; payment ambiguity records its cause; status checks record what happened (answered / timed out / rejected, counted or not) |
| `backend/aorts/banking/remitter/main.py`, `contracts/requests.py`, `app/local_stack.py` | pass `experiment_id`; `StatusEvidence.evidence_id`; new tables in the test truncation list |
| `backend/aorts/integration/v11.py` | **new**: v1.1 producers (`export_event`, `export_status_result`, `WindowProducer`/`export_window`) and the `Delivery` loop |
| `backend/aorts/adapters/controller_client_v11.py` | `request` / `gate` split so Track B can re-read rows under lock between them |
| `backend/aorts/banking/dispatcher/service.py` | `Dispatcher(v11=…)`: v1.1 tick with the execution-time gate |
| `tests/trackb/integration/test_v11_cross_track.py` | **new**: 18 DB-backed cross-track tests (09R list 1–25) |

Payment value semantics, debit/credit rules, reversal authority and the safety gate are
unchanged. The only behavioural fix to existing code is the shared debit event id.

### Migration 0002 (additive only)

- `transactions.experiment_id`, `transactions.check_in_flight_id`: nullable. Older rows keep
  NULL. No backfill.
- `evidence_records`: one row per signed response received. A check constraint means
  `verified` requires a key id and a signature. Kinds: `ORIGINAL_PAYMENT_RESPONSE`,
  `STATUS_CHECK_RESPONSE`, `RECONCILIATION_RECORD`.
- `v11_telemetry_windows`: unique per (experiment, source, sequence) and per
  (experiment, source, window_start). Requires `window_end > window_start`.
- `v11_delivery_state` (cursor), `v11_dead_letters` (unique per stream and ref).
- The downgrade drops exactly these objects and never touches ledger rows. It was verified up →
  down → up on a scratch database, with zero drift between the ORM and the migrated schema. It was
  applied only to disposable databases.

### Producers

- **Transaction events.** `export_event` maps an outbox row to a v1.1 `TransactionEvent`:
  - source `trackb-ledger`; `source_sequence` is the outbox id;
  - `state_version` is the ledger version;
  - `check_id` and counted attempts come from what the ledger recorded;
  - the evidence reference comes only from a stored `EvidenceRecord`, with
    `response_version` = the signed response timestamp.

  It then calls the Stage 03 `adapt_outbox_event`. Rows that cannot be expressed come back as
  `Incompatible` and are dead-lettered with the reason, never coerced.
- **Status-check results.** `export_status_result` builds `StatusCheckResult` from the stored
  `STATUS_CHECK_RESPONSE` evidence, including the signature as received and whether it verified.
- **Telemetry.** `WindowProducer.close`:
  - measures one window from the ledger (sends, responses, timeouts, check releases and
    answers, evidence latencies, live outstanding);
  - takes the per-source sequence under a row lock; each window starts where the previous one ended;
  - runs Track B's own `TelemetryValidator`, cross-checked against the switch's independent
    measurement;
  - persists the result.

  `export_window` emits `ValidatedRouteTelemetry`. The completed-rate split is stated only when
  the reported total equals the measured one. REJECTED windows are never delivered.

### Delivery

`Delivery` delivers telemetry and events in outbox order. For each event it posts the status
result first, then the event.

| controller response | effect |
|---|---|
| 204 | delivered |
| 200 duplicate | counted as a duplicate; harmless |
| 429, 5xx, transport error | exponential backoff; the cursor stops and the event is retried |
| other refusal (e.g. 409 contradiction) | dead letter |
| adapter `Incompatible` | dead letter (`EXPERIMENT_MISMATCH` rows are skipped) |

Delivery is at least once with idempotent intake. It is not exactly once.

### Execution-time gate (Track B dispatcher)

For each route with candidates, `Dispatcher.tick_v11`:

1. sends a `DecisionRequest`;
2. re-reads the candidate rows;
3. applies `gate_decision`, which checks identity, expiry, timing policy, state versions,
   terminal and in-flight state, the rate allowance and the credit allowance;
4. applies the existing `PreDispatchGate`;
5. dispatches each admitted check through the ledger's compare-and-set `start_check`.

What it does with each controller outcome:

| outcome | dispatch | labelled source |
|---|---|---|
| `DECISION` | admitted rows only | `controller` |
| outcomes that permit fallback (controller unavailable, malformed, …) | Track B's fixed fallback, oldest first | `trackb_fallback`, no decision id |
| `EXPLICIT_FAIL_CLOSED` / warming up | **zero** checks | `fail_closed_honoured` |

Every tick is audited as `RECOVERY_DECISION_V11`. It records the outcome, source, decision id,
admitted rows, and the gate and pre-dispatch refusals.

## Verification (Stage 09R)

The database was disposable: a `postgres:16-alpine` container on `127.0.0.1:55433` with tmpfs
storage. All runs used `AORTS_REQUIRE_DB=1`, so a missing database fails the run instead of
skipping it. The command list and results are in the Stage 09R report.

The cross-track tests run the real remitter, switch and beneficiaries over HMAC-signed HTTP on
PostgreSQL, and the real `ControllerCore` (Stage 06 estimator, Stage 07 look-ahead, Stage 08
optimizer) over its FastAPI transport. To reach a positive decision within seconds, the tests use
a declared, compressed estimator config (`FAST`). Telemetry windows are aligned to whole
wall-clock seconds, so the switch's per-second cross-check covers the same interval as each
window.

## Known semantics (documented, not hidden)

- **Not representable in v1.1, so dead-lettered with the reason:**
  - `REVERSAL_INITIATED`, which is committed atomically with `REVERSED`; the
    `REVERSAL_COMPLETED` event carries the fact, with a version gap of 1;
  - `RESOLVED_BY_RECONCILIATION`;
  - a `NOT_FOUND` answer to the original payment.
- **`correlation_id`** falls back to the payment's idempotency key when the event has none.
- **`response_version`** is the signed response timestamp. The switch has no separate response
  version.
- **Telemetry trust** is `AUTHENTICATED`: windows are measured in process by the remitter, which
  owns the ledger.
- **Lost-confirmation confound.** A lost response is a timeout for the remitter, but the switch
  sees a completed payment. The cross-check therefore reports a timeout-rate deviation for such
  windows. This is the expected behaviour of the validator, not a defect.
- **Flagged windows are gaps.** A window the validator flags (for example after a genuine latency
  spike) never becomes trusted. The estimator sees it as a sequence gap and degrades until the
  gap ages out.

## Merge plan (needs both developers' approval)

1. Track B reviews the migration, the ledger and remitter provenance changes, `integration/v11.py`
   and the dispatcher's v1.1 tick, and decides whether to adopt them on `feature/backend`.
2. A joint PR into `developer` runs the full suites against a live PostgreSQL database.
   This branch is not to be merged or pushed without that approval.
