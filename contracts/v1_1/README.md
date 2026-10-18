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
windows, versions, ledger states, evidence and attempt counts, which this contract forbids. A future
adapter must start from richer Track B data (its internal outbox events and validator output) and
report non-representable fields instead of filling them.

## Open integration issues (non-blocking)

1. Teammate (Track B) review of every model and of decisions 3, 4, 6, 7, 11.
2. Canonical signing format and key management.
3. Verification of the NPCI circulars and RBI/2019-20/67, including stop error codes and the
   first-check reference point.
4. Event-transport adapter (decision 8) and DLQ policy.
5. `ownership.yml` still lists non-existent `security/` and `experiments/` paths and does not
   guard `tests/`.
