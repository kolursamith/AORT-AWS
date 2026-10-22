# Contract v1.1 — provisional working interface baseline

`contracts/v1_1/` sits beside the frozen `contracts/v1/`. v1 is unchanged and its endpoints keep
their fail-closed behaviour. v1.1 is the **working interface baseline** accepted by Samith Kolur on
2026-10-17 (Stage 0C). It is **not** a final cross-track agreement and **not** evidence of
regulatory compliance. Cross-track changes are recorded as integration issues.

The models validate structure and per-message consistency only. They never authenticate,
verify signatures, read clocks, touch databases or make network calls.

## Accepted decisions (Stage 0B/0C)

| # | Decision |
|---|---|
| 1 | v1.1 is a new package; `contracts/v1/` stays frozen. |
| 2 | `route_id` is opaque; its endpoints come from a `RouteDescriptor` registry entry. |
| 3 | Research default: a check that was dispatched but whose receipt is uncertain is counted; a check definitely rejected before dispatch is not. This is **not** a verified NPCI rule. |
| 4 | The banking backend owns authoritative `deadline_at`. Synthetic research deadlines are labelled through the referenced `TimingPolicy.verification_status`. |
| 5 | When safe capacity or trust evidence is missing, decisions fail closed with zero budget. A configured conservative fallback may be added later, only with tests. |
| 6 | Reversal is one atomic PostgreSQL operation (no `REVERSAL_INITIATED` state). |
| 7 | Evidence carries signing metadata only. No HMAC/signature code exists yet; the canonical signing format below is a proposal still to be agreed. |
| 8 | HTTP contracts for local integration; an event-transport adapter comes later. Models are transport-neutral. |
| 9 | Flagged-but-authentic telemetry is kept apart from trusted capacity-estimation inputs. |
| 10 | Track A tests in `tests/intelligence/`; shared contract tests in `tests/contracts/`. |
| 11 | Attack generation and security enforcement belong to Track B (no code relocated yet). |
| 12 | The first-check reference point is an explicit `TimingPolicy` field. |
| 13 | Unverified NPCI/RBI details are research assumptions until authoritative sources are checked. |
| 14 | Tier 2 dynamic timing exists only in experimental mock-switch regimes. |
| 15 | v1 endpoints and fail-closed behaviour are retained. |
| 16 | Python 3.11 locally, Python 3.12 compatible in CI; no new dependencies. |

## Common rules (every model)

- `extra="forbid"`, `frozen=True`; sequences are tuples, so instances are deeply immutable.
- Top-level messages carry `schema_version: "1.1"` (required, exact).
- Identifiers: 1–128 characters matching `^[A-Za-z0-9][A-Za-z0-9._:-]*$`.
- Timestamps must be timezone-aware and are normalised to UTC. There are no clock defaults.
- Numbers are strict (no bools or numeric strings); NaN and infinity are rejected.
- `null` means *not observed*; `0` means *measured zero*.
- Money is `amount_minor` (integer minor units, > 0) with an ISO-4217-style `currency`.

## Model inventory

| Area | Models and enums |
|---|---|
| Identity and events | `RouteDescriptor`, `TransactionEvent`, `TransactionEventType`, `FinancialOutcome`, `LedgerState`, `RecoveryWorkflowState`, `EvidenceReference`, `EvidenceKind`, `EvidenceSignature` |
| Telemetry | `RouteTelemetry`, `ObservationScope`, `ValidatedRouteTelemetry`, `TelemetryTrustStatus`, `TelemetryValidationFlag`, `CrossCheckStatus`, `SourceSequenceMark`, `RouteTwinSnapshot`, `FreshnessStatus` |
| Projection | `TransactionProjection`, `QuarantineReason` |
| Decisions | `DecisionRequest`, `RecoveryDecision`, `DecisionMode`, `DecisionReason`, `TransactionVersionReference` |
| Status checks | `StatusCheckRequest`, `StatusCheckResult`, `CheckResultKind`, `ErrorResponse`, `ErrorCode` (`errors.py`) |
| Experimental credits | `CreditLease`, `CreditConsumption`, `ExperimentalCheckLimits`, `Tier2LimitClass` |
| Timing | `TimingPolicy`, `TimingRegime`, `VerificationStatus`, `FirstCheckReference`, `DeadlineRule`, `DeadlineRuleKind` |

## Three distinct state concepts

1. **Ledger state** — authoritative, PostgreSQL, enforced by Track B. Only this layer moves money.
2. **Financial outcome** — observed evidence about the beneficiary credit. `UNKNOWN` is not `FAILURE`.
3. **Recovery workflow** — the Track A projection's view of recovery progress. Non-authoritative.

The tables below are specifications; nothing in this package executes them.

### Ledger state (Track B; compare-and-swap on expected `version` and allowed state)

| From | Trigger | Required evidence | To | Version | Money |
|---|---|---|---|---|---|
| — | Valid payment accepted | Unique idempotency key | `DEBITED` | 0 → 1 | Debit |
| `DEBITED`, `RECONCILIATION_PENDING` | Authoritative success | Verified success evidence | `CONFIRMED_SUCCESS` | v → v+1 | None |
| `DEBITED`, `RECONCILIATION_PENDING` | Authoritative failure | Verified failure evidence | `CONFIRMED_FAILED` | v → v+1 | None |
| `CONFIRMED_FAILED` | Reversal posting (atomic with state change and outbox row) | Committed failure; unique `operation_id` | `REVERSED` | v → v+1 | Credit back, exactly once |
| `DEBITED` | Check limit used, or deadline passed | Authoritative counted attempts or policy deadline | `RECONCILIATION_PENDING` | v → v+1 | None |
| `CONFIRMED_SUCCESS`, `REVERSED` | Close | — | `CLOSED` (terminal) | v → v+1 | None |

Forbidden: any transition caused by a timeout; `DEBITED → REVERSED`; `CONFIRMED_SUCCESS ↔
CONFIRMED_FAILED` (contradicting evidence is recorded and handed to reconciliation).

Outcomes allowed per ledger state (enforced on `TransactionEvent` and `TransactionProjection`):

| Ledger state | Allowed financial outcome |
|---|---|
| `DEBITED`, `RECONCILIATION_PENDING` | `UNKNOWN`, `PENDING` |
| `CONFIRMED_SUCCESS` | `SUCCESS` |
| `CONFIRMED_FAILED`, `REVERSED` | `FAILURE` |
| `CLOSED` | `SUCCESS`, `FAILURE` |

### Financial outcome

| From | Trigger | To |
|---|---|---|
| `UNKNOWN` | Evidenced pending answer | `PENDING` |
| `UNKNOWN`, `PENDING` | Verified authoritative result | `SUCCESS` or `FAILURE` |
| any | Payment timeout, check timeout, transport error, credit or rate rejection | unchanged |
| `SUCCESS`, `FAILURE` | Contradicting evidence | unchanged; conflict recorded |

`SUCCESS`, `FAILURE` and `PENDING` always require an `EvidenceReference`.

### Recovery workflow (Track A projection)

| From | Trigger (owner) | To |
|---|---|---|
| — | `PAYMENT_INITIATED` (B) | `ACTIVE` |
| `ACTIVE` | On-time authoritative response (B) | `CLOSED` |
| `ACTIVE` | `PAYMENT_TIMED_OUT` (B); outcome stays `UNKNOWN` | `AMBIGUOUS` |
| `AMBIGUOUS`, `ESCALATED` | `STATUS_CHECK_DISPATCHED` (B) — not the decision itself | `CHECK_IN_FLIGHT` |
| `CHECK_IN_FLIGHT` | Answer `PENDING`/`UNKNOWN`, check timeout, upstream error (B) | `AMBIGUOUS` (or `ESCALATED` if flagged) |
| `AMBIGUOUS` | `STATUS_CHECK_REJECTED` (B); not counted | unchanged |
| `CHECK_IN_FLIGHT`, `AMBIGUOUS` | Authoritative result committed, including a late original response (B) | `CLOSED` |
| `AMBIGUOUS` | Clock reaches `deadline_at − escalation_window_s` (A) | `ESCALATED` |
| `AMBIGUOUS`, `ESCALATED` | `RECONCILIATION_HANDOFF` (B) | `HANDOFF_RECONCILIATION` |
| any non-terminal | Conflicting evidence or events, identity mismatch (A) | `QUARANTINED` |
| `HANDOFF_RECONCILIATION`, `QUARANTINED` | `RECONCILIATION_RESOLVED` (B) | `CLOSED` |

Escalation raises ranking priority and alerts; in Tier 1 it never adds checks beyond the cap.

### Event-type constraints enforced by `TransactionEvent`

| Event type | Ledger state | Outcome | Other |
|---|---|---|---|
| `PAYMENT_INITIATED` | `DEBITED` | `UNKNOWN` | `state_version == 1`, no counted checks |
| `PAYMENT_TIMED_OUT` | `DEBITED` | `UNKNOWN` | otherwise `TIMEOUT_CANNOT_IMPLY_OUTCOME` |
| `PAYMENT_RESPONSE_RECEIVED` | not `RECONCILIATION_PENDING`/`CLOSED` | evidenced | evidence kind `ORIGINAL_PAYMENT_RESPONSE` |
| `STATUS_CHECK_DISPATCHED` | `DEBITED` | `UNKNOWN`/`PENDING` | `check_id` |
| `STATUS_CHECK_ANSWERED` | any consistent | matches `check_result` | `check_id`, `check_result`; evidenced results use kind `STATUS_CHECK_RESPONSE` |
| `STATUS_CHECK_REJECTED` | `DEBITED` | `UNKNOWN`/`PENDING` | `check_id`; `result_code` pre-dispatch or dispatcher-declared |
| `STATUS_CHECK_TIMED_OUT` | `DEBITED` | `UNKNOWN`/`PENDING` | `check_id`; `result_code` `CHECK_TIMEOUT` or `UPSTREAM_UNAVAILABLE` |
| `REVERSAL_COMPLETED` | `REVERSED` | `FAILURE` | — |
| `RECONCILIATION_HANDOFF` | `RECONCILIATION_PENDING` | `UNKNOWN`/`PENDING` | — |
| `RECONCILIATION_RESOLVED` | confirmed or reversed | `SUCCESS`/`FAILURE` | evidence kind `RECONCILIATION_RECORD` |
| `TRANSACTION_CLOSED` | `CLOSED` | `SUCCESS`/`FAILURE` | — |

No event type is a command. Financial operations are internal to Track B.

## Telemetry semantics

- Rates are requests per second averaged over the half-open window
  `[observation_window_start, observation_window_end)`; `observation_timestamp ≥ window_end`.
- `observation_scope` defines what can be seen. `REMITTER_OWN_TRAFFIC` (the only scope a real bank
  has) covers the bank's own requests, and `queue_depth` is its own outstanding requests.
  `SWITCH_ROUTE`/`SWITCH_GLOBAL` exist only for the mock switch, Tier 2 and validator cross-checks.
- `observed_throughput_rps` counts completed payments **and** checks; the optional split must sum
  to it.
- Latencies are over completed responses; they must be `null` when `latency_sample_count` is 0.
- `timeout_rate = timeout_count / timeout_denominator_count`; `null` if the denominator is 0.
- Sequence numbers are scoped per (`experiment_id`, `source_id`).
- Authenticity (`trust_status`) is decided by Track B at the trust boundary. Statistical findings
  are `validation_flags`. Only `AUTHENTICATED` telemetry may reach the twin; flagged samples go to
  `RouteTwinSnapshot.latest_flagged_observation`, never to the trusted slot.

## Errors and check counting (`errors.py`)

Every `ErrorCode` has an HTTP status (`HTTP_STATUS`) and a counting rule (`CHECK_COUNTING`):

| Rule | Codes | `ErrorResponse.counts_against_limit` |
|---|---|---|
| `COUNTED` | `CHECK_TIMEOUT`, `EVIDENCE_SIGNATURE_INVALID` | must be `true` |
| `DECLARED_BY_DISPATCHER` | `UPSTREAM_UNAVAILABLE`, `INTERNAL_ERROR` | `true` or `false`, never `null` |
| `NOT_COUNTED` | rejections before dispatch (rate limit, credits, auth, schema, eligibility, …) | must be `false` |
| `NOT_APPLICABLE` | intake outcomes (sequence, staleness, duplicates, evidence) | must be `null` |

A transport failure yields `UNKNOWN`; only verified `AUTHORITATIVE_FAILURE` evidence is a financial
failure. `StatusCheckResult.is_authoritative` is true only when Track B has verified the evidence.

## Proposed canonical signing format (not yet agreed; no code implements it)

The signer serialises the fields named in `EvidenceSignature.signed_fields`, taken from the
evidence record, as compact UTF-8 JSON with keys sorted and no insignificant whitespace, then signs
those bytes with `signature_alg` and `key_id`. Track B owns keys and verification.

## Timing policy and research assumptions

`TimingPolicy` is configuration; values are never hard-coded. Enforced rules:

- `TIER1_COMPLIANT` requires `verification_status = VERIFIED`. Policies using assumed values must
  use `RESEARCH_SCALED`.
- `TIER2_EXPERIMENTAL` can never be `VERIFIED`.
- `escalation_extra_checks > 0` only in `TIER2_EXPERIMENTAL`.
- `REGULATORY_CALENDAR` deadlines require a `VERIFIED` policy; no calendar calculation exists.

| Rule from project documents | Value | Status |
|---|---|---|
| First status check delay (NPCI circular, 26 Apr 2025, via news sources) | 90 s | Research assumption; reference point unconfirmed |
| Maximum checks (same) | 3 within 2 h | Research assumption |
| Stop after specified error codes (same) | codes not listed | Unspecified |
| Resolution deadline (RBI/2019-20/67, as cited) | T+1, ₹100/day | Research assumption; category and calendar unverified |
| Check spacing (report Table 12) | 90 s | Research parameter |
| Test deadline (report) | 60 min | Research parameter |
| Client / check timeout (report) | 15 s / 10 s | Research parameters |
| Tier 2 CALM / NORMAL / STORM (proposal §6.2) | 30 s ≤6, 90 s 3, 180 s 2 | Experimental, mock switch only |

## Compatibility with the team contract v1

`contracts/v1` (the team contract served by Track B's `/v1` endpoints) is untouched; v1.1 sits beside
it and is not wire-compatible. Differences, all within the new package:

| v1 (team) | v1.1 |
|---|---|
| lowercase enums: `credited` / `not_credited` / `unknown` | `SUCCESS` / `FAILURE` / `UNKNOWN` plus `PENDING` |
| `workflow_state` on events (`initiated`, `ambiguous`, `check_pending`, `resolved`, `escalated`) | ledger state on events; workflow only in `TransactionProjection` |
| 5 event types | 12 committed-fact event types |
| `bank_id` | `remitter_bank_id` and `beneficiary_bank_id` |
| `version` (>= 1), `occurred_at` | `state_version` plus `source_id`/`source_sequence`, `event_timestamp` |
| no evidence reference, check id, counted attempts or deadline | all present and validated |
| telemetry `timestamp` only; all measurements required | observation window, scope, sample count; nullable measurements |
| `StatusCheckResult.outcome` / `authoritative` / `evidence_source_id` | `check_result` (incl. `PENDING`), evidence reference and signature metadata, `evidence_verified` |
| `RecoveryDecision` predictions required | predictions nullable until a model exists; mode, reason code, versions, watermark |

No v1 to v1.1 adapter is provided. Translating v1 messages would require inventing observation
windows, versions, ledger states, evidence and attempt counts, which this contract forbids. The
Stage 03 adapters (`adapters.py`, below) start from richer Track B data (its internal outbox events,
validator output and status-check outcomes) and report non-representable fields instead of filling
them.

## Wire protocol v1.1 (Stage 03; `protocol.py`)

Proposed only. **None of these endpoints exists in Track B yet**; `/v1` stays as it is. Bodies are
the v1.1 models above, serialized by `protocol.encode` as compact UTF-8 JSON with sorted keys and
UTC `Z` timestamps (deterministic bytes). `protocol.decode` refuses instead of repairing: a missing
or different `schema_version` is `UNSUPPORTED_SCHEMA_VERSION`; any other structural problem,
unknown fields included, is `SCHEMA_INVALID`. Errors are `ErrorResponse` bodies with the HTTP
status from `HTTP_STATUS`, built by `protocol.error_response` so that retryability and check
counting always follow the shared tables.

| Name | Method and path | Producer → consumer | Body → response | Success | Idempotency |
|---|---|---|---|---|---|
| telemetry_intake | `POST /v1.1/telemetry` | Track B trust boundary → controller | `ValidatedRouteTelemetry` → — | 204 | `(experiment_id, source_id, sequence)`; identical resend 200 `DUPLICATE_SEQUENCE`, different body 409 |
| transaction_event_intake | `POST /v1.1/transaction-events` | Track B outbox relay → controller | `TransactionEvent` → — | 204 | `event_id` and `(transaction_id, state_version)` content; resend 200 `DUPLICATE_EVENT`, different content 409 `CONFLICTING_EVENT` |
| status_check_result_intake | `POST /v1.1/status-check-results` | Track B dispatcher → controller | `StatusCheckResult` → — | 204 | `check_id`; resend 200 `DUPLICATE_EVENT`, different content 409 `DUPLICATE_CHECK_ID`. Sensing only: the matching `TransactionEvent` changes the projection |
| recovery_decision | `POST /v1.1/recovery/decisions` | Track B dispatcher → controller | `DecisionRequest` → `RecoveryDecision` | 200 | new `decision_id` per call; safe to retry |
| controller_health | `GET /v1.1/health` | controller → Track B | — → `ControllerStatus` | 200 | read-only |
| controller_readiness | `GET /v1.1/ready` | controller → Track B | — → `ControllerStatus` | 200, or 503 with `ready=false` and reasons | read-only |

Transport: HTTP/1.1 on the isolated local network, `Content-Type: application/json`. Every request
is authenticated with Track B's existing HMAC headers (`X-AORTS-Key-Id`, `-Timestamp`, `-Nonce`,
`-Signature`; `security/request_auth.py`); the authenticated identity, never a body field, is the
caller. **Retryable** (same message, after backoff): `CHECK_TIMEOUT`, `UPSTREAM_UNAVAILABLE`,
`RATE_LIMITED`, `CREDIT_EXHAUSTED`, `OUT_OF_ORDER_SEQUENCE`, `INTERNAL_ERROR`, and transport
failures (connect error, timeout, 5xx). Every other code is **terminal** for that message:
fix the producer, never resend unchanged. A fail-closed decision is a 200 response, not an error.

### Decision request rules (controller side: `validate_decision_request`)

The request is exactly `DecisionRequest`: `schema_version`, `experiment_id`, `route_id`,
`requested_at` (UTC) and `min_telemetry_watermark`. The starter fields Track B sends today
(`estimated_capacity_rps`, `confidence`, `pending_transaction_ids`) are refused by name: the
controller derives capacity from its Route Twin and candidates from its Transaction Twin. Checks:
experiment served (`EXPERIMENT_MISMATCH`), route registered (`ROUTE_IDENTITY_MISMATCH`),
`requested_at` neither older than the configured age (`STALE_OBSERVATION`) nor beyond the skew
(`FUTURE_TIMESTAMP`; both bounds inclusive). If the twin has not reached the requested watermark
(`lagging_sources`), the controller answers an explicit `FAIL_CLOSED`/`TELEMETRY_STALE` decision
(`build_fail_closed_decision`), not an error.

### Decision execution gate (Track B side: `gate_decision`, requirements X1–X11)

`gate_decision` is the pure, testable statement of what Track B's dispatcher must check, in this
order, before any status check is dispatched. The real checks must run inside Track B against
locked PostgreSQL rows; this function is their executable specification.

| # | Requirement | Refusal | Scope |
|---|---|---|---|
| — | Body decodes as a strict v1.1 `RecoveryDecision` (no repair, no rounding, no trimming) | `MALFORMED` | decision |
| X4 | Authenticated transport identity is an authorized controller | `UNAUTHORIZED_CONTROLLER` | decision |
| X2 | `experiment_id` equals the request's | `EXPERIMENT_MISMATCH` | decision |
| X1 | `route_id` equals the request's | `ROUTE_MISMATCH` | decision |
| X3 | `created_at ≤ now + skew` and `now < expires_at` (expired at exactly `expires_at`) | `NOT_YET_VALID`, `EXPIRED` | decision |
| X10 | `timing_policy_id` equals the policy Track B enforces | `TIMING_POLICY_MISMATCH` | decision |
| — | `FAIL_CLOSED` / `WARMING_UP` → dispatch nothing | — | decision |
| X5 | Selected transaction exists, in this experiment and on this route | `UNKNOWN_TRANSACTION`, `WRONG_EXPERIMENT`, `WRONG_ROUTE` | transaction |
| X6, X11 | Not terminal (checked before the version, so a terminal outcome always blocks) | `TERMINAL` | transaction |
| X7 | Current state version equals `state_versions[i].version` | `STALE_VERSION` | transaction |
| X6 | No check already in flight | `CHECK_IN_FLIGHT` | transaction |
| X10 | Counted attempts below `max_counted_checks`; `eligible_from ≤ now ≤ eligible_until` | `CHECK_LIMIT_REACHED`, `NOT_YET_ELIGIBLE`, `WINDOW_CLOSED` | transaction |
| X8 | At most `floor(budget_rps × dispatch_window_s)` admitted (the model already bounds the selection by `budget_rps × validity`) | `OVER_BUDGET` | transaction |
| X9 | Within the route's remaining rate allowance and Tier 2 credits | `RATE_LIMITED`, `CREDIT_EXHAUSTED` | transaction |

A refused transaction does not refuse the rest of the decision. Refusals with an exact v1.1 code
are listed in `ERROR_CODE_FOR_REFUSAL`; `NOT_YET_VALID`, `TIMING_POLICY_MISMATCH`, `TERMINAL`,
`CHECK_IN_FLIGHT`, `WINDOW_CLOSED` and `OVER_BUDGET` have none and stay backend-local (logged and
audited by Track B). Admitted transactions may receive a **status check only**: no decision, gate
result or fallback ever authorizes a reversal, credit, debit or settlement.

### Fallback and timing-policy decision register (provisional; requires joint sign-off)

| # | Case | Outcome | Controller-directed checks | Track B's own fallback (`fallback_budget_per_route`, today 2) |
|---|---|---|---|---|
| M1 | Valid explicit `FAIL_CLOSED` | `EXPLICIT_FAIL_CLOSED` | 0 | **Not permitted.** The controller has said recovery traffic is unsafe |
| M2 | Valid `WARMING_UP` | `WARMING_UP` | 0 | Not permitted |
| M3 | Controller unreachable or 5xx | `CONTROLLER_UNAVAILABLE` | 0 | Permitted, labelled `fallback=true` with the reason, under all Track B gates |
| M4 | Controller timeout | `CONTROLLER_TIMEOUT` | 0 | as M3 |
| M5 | Malformed response (including any selection beyond its own budget) | `MALFORMED_RESPONSE` | 0 | as M3, plus an alert; the response is never repaired |
| M6 | Expired decision | `DECISION_EXPIRED` | 0 | as M3 after one fresh request |
| M7 | Stale state versions | `DECISION` with per-transaction `STALE_VERSION` | the rest of the decision | not needed |
| M8 | Wrong route/experiment, unauthorized controller, timing-policy mismatch | the matching outcome | 0 | as M3, plus an alert |

Track B's current `ResilientController` applies the fixed fallback on **every** failure, and a
controller cannot express it (a `FAIL_CLOSED` decision with budget 2 is malformed). M1/M2 are the
required behaviour change for Track B.

Timing policies are referenced by `TimingPolicyReference = (policy_id, sha256 of the canonical
policy bytes)`. The controller advertises its reference in `ControllerStatus.timing_policy`; Track B
must refuse to use the controller (M8) when `timing_policy_mismatches` reports a different ID or the
same ID with different content. Distinct research configurations simply have distinct IDs. Neither
side's policy is verified NPCI/RBI compliance (`verification_status` is never `VERIFIED` here).

| | Track A research policy (`report-table12-research`) | Track B defaults (`app/config.py`) |
|---|---|---|
| First check | 90 s after initiation | `check_backoff_base_seconds` = 1 s after ambiguity |
| Spacing | 90 s fixed | exponential `min(30, 1·2^(n−1))` s |
| Counted checks | 3 | 6 (`max_checks_per_transaction`) |
| Window / deadline | 7200 s / 3600 s | none / 120 s (`deadline_seconds`) |
| Expressible as v1.1 `TimingPolicy` | yes | **no** while spacing grows; yes when base = max (`express_trackb_timing_policy`) |

## Track B → v1.1 adapters (Stage 03; `adapters.py`)

Pure functions over the JSON shapes Track B produces at `feature/backend@8b863da`. Each takes a
separate `boundary` mapping with the extra metadata v1.1 needs; at 8b863da Track B emits none of it,
so **every real Track B record is rejected today** with an `Incompatible` result listing the missing
fields. Nothing is inferred: no window from a timestamp, no sequence from a clock, no verdict from a
boolean, no outcome from a timeout. `UNKNOWN` never becomes `FAILURE`.

### Outbox event mapping (`OUTBOX_RULES`)

| Track B event (previous → new state) | v1.1 event | Status | Required extra metadata |
|---|---|---|---|
| `PAYMENT_DEBITED` (— → DEBITED) | `PAYMENT_INITIATED` | direct | experiment, source, sequence, `deadline_policy_id` |
| `PAYMENT_AMBIGUOUS` (DEBITED → AMBIGUOUS) | `PAYMENT_TIMED_OUT` or `PAYMENT_RESPONSE_RECEIVED` (PENDING) | **ambiguous in Track B**: timeout and pending answer share it | `ambiguity_cause`; evidence for PENDING |
| `CHECK_STARTED` (AMBIGUOUS → CHECK_IN_FLIGHT) | `STATUS_CHECK_DISPATCHED` | direct | `check_id` (Track B has none) |
| `CHECK_RESULT` (CHECK_IN_FLIGHT → AMBIGUOUS) | `STATUS_CHECK_ANSWERED` (PENDING/UNKNOWN), `_TIMED_OUT` or `_REJECTED` | **overloaded**: inconclusive answer, transport failure and switch refusal share it | `check_fact` (kind + result or code), `check_id` |
| `PAYMENT_SUCCEEDED` / `PAYMENT_FAILED` (unresolved → CONFIRMED_*) | `STATUS_CHECK_ANSWERED` (AUTHORITATIVE_*) or `PAYMENT_RESPONSE_RECEIVED` | decided by evidence kind | evidence reference (Track B stores only `evidence_source`), `check_id` for checks |
| `REVERSAL_COMPLETED` (REVERSAL_INITIATED → REVERSED) | `REVERSAL_COMPLETED` | direct; requires the committed `payload.operation_id` | evidence reference of the confirmed failure |
| `PAYMENT_CLOSED` (CONFIRMED_SUCCESS → CLOSED) | `TRANSACTION_CLOSED` | direct | evidence reference |
| `ESCALATED` (AMBIGUOUS/CHECK_IN_FLIGHT/QUARANTINED → HANDOFF_UDIR) | `RECONCILIATION_HANDOFF` | direct | — |
| `CHECK_RESULT` (CONFIRMED_FAILED → REVERSAL_INITIATED) | — | not representable (decision 6); committed atomically with REVERSED, so the twin sees a version gap of 1 | — |
| `PAYMENT_CLOSED` (HANDOFF_UDIR → RESOLVED_BY_RECONCILIATION) | — | not representable: one Track B terminal state combines the reconciliation result, settlement or reversal, and closure | joint decision |
| `QUARANTINED`, `CHECK_RESULT` (QUARANTINED → AMBIGUOUS) | — | not representable: backend quarantine is a Track A projection state in v1.1 | joint decision |

Common extra fields: `experiment_id`, `source_id`, `source_sequence` (the outbox row id is the
natural monotonic sequence), `beneficiary_bank_id` (Track B puts it only on the debit event),
`correlation_id` when Track B's is null. Track B's `checks_used` is copied to
`counted_check_attempts`; Track B increments it when the answer is recorded, so on
`STATUS_CHECK_DISPATCHED` the in-flight attempt is not yet included (the twin's limit then
under-allows by at most the in-flight check, which it already blocks).

### Telemetry (`adapt_validated_telemetry`)

Measurements come from Track B's observation (`latency_p50_ms`, `latency_p95_ms`,
`throughput_tps` → `observed_throughput_rps`, `timeout_rate`, `backlog` → `queue_depth`);
`observation_timestamp` is Track B's own `timestamp`. `health_score` has no v1.1 counterpart and is
dropped. Track B must add: `experiment_id`, `observation_scope`, the observation window,
`payment_rps`, `check_rps`, `latency_sample_count`, a structured `trust_status`, structured
`validation_flags` and `cross_check_status` (today only free-text `reasons`), `validator_id`,
`validator_version` and `validated_at`. `REJECTED` samples are never forwarded; `TRUSTED` requires
`AUTHENTICATED` and no flags; `SUSPICIOUS` requires at least one flag.

### Status-check outcomes (`adapt_status_check`)

| Track B outcome | v1.1 message | Counts |
|---|---|---|
| `admitted=false` (HTTP 429/403) with a known code | `ErrorResponse` (`RATE_LIMITED`, `CREDIT_EXHAUSTED`, `AUTHENTICATION_FAILED`, `AUTHORIZATION_DENIED`) | no |
| `admitted=false` without a known code (e.g. `"REJECTED"`) | rejected as incompatible | — |
| transport error `*Timeout` | `ErrorResponse` `CHECK_TIMEOUT` | yes |
| other transport error | `ErrorResponse` `UPSTREAM_UNAVAILABLE`, declared counted (Track B counts it) | yes |
| answer with a failed signature | `ErrorResponse` `EVIDENCE_SIGNATURE_INVALID` | yes |
| `SUCCESS` / `FAILED` | `StatusCheckResult` `AUTHORITATIVE_*` only with evidence reference, signature metadata and a complete verification record (`status=VERIFIED`, `verifier_id`, `key_id` equal to the signature's, `verified_at`) | yes |
| `PENDING` | `PENDING` with evidence; `evidence_verified` only with that record | yes |
| `NOT_FOUND` | `UNKNOWN` (no record is not a failure) | yes |

Track B's `StatusEvidence.authenticated` boolean alone never makes a result authoritative.

## Track B integration handoff (future Track B work; not done in Stage 03)

1. **Endpoints**: serve or call the six `/v1.1` endpoints above (paths, methods, bodies, statuses
   and idempotency as tabled); keep `/v1` unchanged.
2. **Schemas**: `contracts/v1_1/models.py` and `protocol.ControllerStatus`; validate with
   `protocol.decode`.
3. **Errors**: `ErrorResponse` with `HTTP_STATUS`; retry only the retryable codes.
4. **Authentication**: existing HMAC request signing on every call; authorize the controller by its
   authenticated key identity (X4); the controller authorizes Track B's relay identity.
5. **Telemetry metadata**: the fields listed under Telemetry, produced at the validator boundary.
6. **Evidence metadata**: persist per status response an `evidence_id`, `issuer_id`,
   `response_version`, the response signature (`HMAC-SHA256`, `X-AORTS-Response-Key-Id`, signature,
   signed fields `status_code`, `path`, `timestamp`, `body`) and the verifier record from
   `verify_response`; reference the evidence from the outbox events it causes.
7. **Check identity**: create a `check_id` at `start_check` and carry it on the dispatch job, the
   outbox events and the status result.
8. **Event export**: emit v1.1 `TransactionEvent`s from the outbox relay with `source_id`,
   `source_sequence` = outbox row id, `beneficiary_bank_id` on every event, and structured
   `ambiguity_cause` / `check_fact` instead of free-text reasons.
9. **Decision expiry**: X3 with Track B's clock and a configured skew.
10. **State versions**: X5–X7 under `SELECT … FOR UPDATE` in the same transaction as `start_check`.
11. **Timing policy**: configure a v1.1 `TimingPolicy` (constant spacing, or a jointly extended
    model), compare references with the controller's `/v1.1/ready` (M8).
12. **Fallback**: implement M1–M8; in particular no fixed-budget fallback after an explicit
    `FAIL_CLOSED` or `WARMING_UP`.
13. **Retry and idempotency**: outbox relay is at-least-once; the controller deduplicates by
    `event_id`/`(transaction_id, state_version)`; decision requests are retried, decisions never
    reused after expiry.
14. **PostgreSQL-backed tests** (Track B, `AORTS_REQUIRE_DB=1`): stale-version refusal under
    concurrent `start_check`; terminal-after-selection refusal; no dispatch beyond budget, rate or
    credit; no fallback after `FAIL_CLOSED`; outbox export idempotency under redelivery; check
    identity carried end to end; evidence references resolvable; no financial operation caused by
    any controller response.
15. **Track B source files**: `adapters/controller_client.py`, `adapters/team_routes.py`,
    `adapters/team_contract.py`, `banking/dispatcher/service.py`, `banking/remitter/service.py`,
    `banking/ledger/service.py` (check identity, evidence persistence, structured facts),
    `events/outbox.py` and `events/publisher.py`, `security/telemetry_validator.py`,
    `app/config.py`; plus a migration for check and evidence identifiers.

## Open integration issues (non-blocking)

1. Teammate (Track B) review of every model and of decisions 3, 4, 6, 7, 11.
2. Canonical signing format and key management.
3. Verification of the NPCI circulars and RBI/2019-20/67, including stop error codes and the
   first-check reference point.
4. Event-transport adapter (decision 8) and DLQ policy.
5. `ownership.yml` still lists non-existent `security/` and `experiments/` paths and does not
   guard `tests/`.
6. Track B states `REVERSAL_INITIATED`, `RESOLVED_BY_RECONCILIATION` and backend `QUARANTINED` have
   no v1.1 representation (see the mapping table); a joint decision is needed.
7. No v1.1 error code exists for a timing-policy mismatch, a terminal or in-flight selection, or a
   closed check window; these gate refusals stay backend-local until one is agreed.
