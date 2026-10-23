# Stage 09 cross-track integration (local integration branch)

Branch `integration/stage09-trackb` (local only, not pushed), worktree
`CAD_project/AORT-aws-integration`. Base: Track A `feature/intelligence` at `5024e6c` (Stage 09
controller). Merged: Track B `origin/feature/backend` at `9e736d9`. Merge base `cb89ae3`.

## Merge result

- Textual conflicts: none. The two tracks changed disjoint paths. Only Track B changed
  `pyproject.toml` (alembic pin, ruff settings), CI and the gitleaks configuration.
- Merged test run (separate venv with the merged project's pinned `dev` and `research`
  dependencies): 1,494 passed and 109 skipped. All 109 skips are Track B PostgreSQL tests
  ("PostgreSQL test server not reachable"). No Docker daemon or PostgreSQL server was available,
  so none of them ran and none counts as passed.

## Changes in this branch (additive; Track B financial semantics untouched)

| file | owner | change |
|---|---|---|
| `backend/aorts/adapters/controller_client_v11.py` | Track B path, new file | v1.1 client. It sends a `DecisionRequest` with a bearer token, gates the response with the shared `gate_decision` against Track B `TransactionView` rows, and maps transport failures to `ControllerOutcome` |
| `tests/integration_stage09/test_cross_track_v11.py` | joint | real Track B code against the real Track A app (10 tests) |
| `docs/integration/STAGE09_TRACKB_V11_INTEGRATION.md` | joint | this file |

No ledger model, migration, safety gate, dispatcher or security module was modified.

## Verified, in process and not DB-backed

- The v1.1 client admits the controller's eligible selection. The gate refuses stale versions and
  terminal rows, honours explicit FAIL_CLOSED with no fallback, refuses expired decisions,
  wrong-route rows and timing-policy mismatches, and tells "controller unavailable" (fallback
  permitted) apart from refusals.
- Track B's existing `HttpController` (contracts/v1) works against the controller's preserved
  legacy endpoint. That response is labelled as the starter fallback, never as an AORTS optimizer
  decision.

## Blockers to full v1.1 integration

1. Telemetry. Track B telemetry (`backend/aorts/contracts/fixtures/telemetry_event.json`) has no
   experiment ID, no observation window, no observation scope, no offered-rate split and no
   v1.1 trust metadata. The Stage 03 adapter refuses it (`Incompatible`), and the controller
   cannot be fed real Track B telemetry. Needed from Track B: a v1.1 producer that emits
   `ValidatedRouteTelemetry` from its validator.
2. Transaction events. Track B outbox events carry no experiment ID, no v1.1 event type with
   `state_version` semantics and no verified `EvidenceReference`. The Stage 03 adapter refuses
   them. Persisting authenticated evidence metadata and exact state versions for the outbox
   probably needs a ledger/outbox schema change. Under the current authorization that is a STOP:
   no migration was written.
3. Status-check results. They would need a v1.1 `StatusCheckResult` producer with an evidence
   signature and `evidence_verified`.
4. Execution-time wiring. The v1.1 client is not yet called by Track B's dispatcher. Wiring it in
   requires Track B to read `TransactionView` rows under its row lock and to pass real quota and
   credit allowances.
5. Database. Every PostgreSQL-dependent claim (atomic ledger, duplicate results never causing a
   second financial effect, safety gate under concurrency) is unverified here.

## Proposed merge plan (needs both developers' approval)

1. Track B reviews `controller_client_v11.py` and decides whether to adopt it on
   `feature/backend`.
2. Track B implements v1.1 producers for blockers 1–3, and the schema change if one is needed,
   after explicit approval.
3. A joint PR into `developer` runs the Track B PostgreSQL suite against a live database.
   This integration branch is not to be merged as-is.
