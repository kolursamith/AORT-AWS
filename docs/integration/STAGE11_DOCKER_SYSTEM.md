# Stage 11: the complete AORTS system on local Docker

Synthetic mock environment only. Run on Docker Desktop 29.6.1, Compose v5.2.0, linux/arm64
(Apple Silicon), Compose project `aorts-s11` (its own network and volume). No existing
developer database or volume was touched. The runbook is `infra/aorts/README.md`.

## What was built

* **One image** (`infra/aorts/Dockerfile`, Python 3.11-slim, exact `pyproject.toml` pins) runs
  every entry point as an unprivileged user.
* **Nine services**: PostgreSQL 16, one-shot migrate + seed, switch, beneficiary banks B and C,
  remitter bank service, Track B control API, Track A v1.1 controller, `aorts-loop` (telemetry
  producer + validator, outbox/evidence delivery, v1.1 dispatcher with the execution gate) and
  the Streamlit dashboard. Services are one process each. The remitter-bank identities A1-A3
  share one remitter service, as in Track B's design.
* **New code**:
  * `backend/aorts/integration/worker.py`: the `controller-config` and `loop` services.
  * `backend/aorts/integration/demo.py`: the operator CLI (`traffic`, `ambiguous`, `trace`,
    `wait-resolved`, `decisions`, `summary`, `export`, `replay-outbox`, `e2e`).
  * `loop.controller_config`: the controller and backend share one experiment, route set and
    timing policy.
* **Audit addition**: the v1.1 dispatcher audit row now also records what the controller
  claimed (`controller_reason`, `controller_mode`, `controller_budget_rps`). These fields are
  audit only and never used for action. They were added because the first end-to-end run
  could not show *why* the controller chose zero.

## Defects found and fixed while bringing the system up

| defect | fix |
|---|---|
| The controller container crashed on start (`FileNotFoundError: /run/aorts/controller.json`). Writing the config imported `intelligence.controller.api`, whose module-level app reads `AORTS_CONTROLLER_CONFIG` before the file exists. | The config step runs with that variable unset (`env -u`, Compose command). |
| The dashboard failed with `ModuleNotFoundError: plotly`. The `dashboard` extra lacked plotly and pandas, which the app imports. | Added `plotly==6.4.0` and `pandas==2.3.3` (the research pins) to the extra. |

## End-to-end verification (`demo e2e`, evidence in `docs/integration/evidence/stage11/`)

Four runs on fresh volumes. Every step is read back from PostgreSQL, the controller's
responses or the loop's log.

| run | design | result |
|---|---|---|
| 1 | 3 ambiguous before warm-up; honest 6/s per route (sequential sender); 5 lost confirmations during the honest phase | zero budget honoured; 108 controller DECISIONs with 0 checks. The reason was not recorded, which led to the audit addition above. |
| 2 | as 1, with honest 12/s then 3/s per route (Stage 10 protocol) and the reasons recorded | zero budget honoured; `NO_SAFE_BUDGET` ×91; 0 of 5 resolved. The 5 lost confirmations arriving *during* honest windows were 5 timeouts in one window (sequence 27). The estimator counts that as congestion, so it restarts its regime and discards the demonstrated 12/s; then headroom = 0.85·3 − 3 < 0. |
| 3 | 5 ambiguous created *before* the honest windows (as Stage 10 creates its backlog) | zero budget honoured; `NO_SAFE_BUDGET` ×90; 0 of 5 resolved. Probing the controller live: headroom 0 during the 12/s phase (0.85·L ≤ load), then a cap of 1-2 checks/s in the calm phase, but "no positive budget improves on zero". With no check outcomes observed yet, the conservative benefit bound (90 % lower bound of a Beta(1,4) prior) makes 5 candidates worth less than `min_improvement`. This is a cold start. |
| 4 | 30 ambiguous before the honest windows (the Stage 10 backlog size), then 12/s → 3/s | **zero budget honoured** (WARMING_UP before warm-up); then **30 controller decisions `SAFE_BUDGET_SELECTED`, 30 checks dispatched, 30 verified signed STATUS_CHECK_RESPONSE, 30 of 30 payments CLOSED**; 6 `NO_SAFE_BUDGET`. |

Nothing was relaxed between runs. Runs 2-4 differ only in workload (honest rate profile and
backlog size), not in controller, estimator, optimizer or gate configuration. Runs 2 and 3 are
real zero-budget outcomes of the safe controller, and they are reported as such (see
`docs/final/LIMITATIONS.md`).

### The 17 steps, for one payment (`trace-TXNA1-926291FB505F47A9.json`, run 4)

| # | step | evidence |
|---|---|---|
| 1-2 | synthetic payment initiated; beneficiary B commits | ledger v1 `PAYMENT_DEBITED`; switch credited (mock beneficiary logic) |
| 3-4 | remitter response lost → genuine ambiguity | v2 `PAYMENT_AMBIGUOUS`, `ambiguity_cause = LOST_RESPONSE`; financial outcome UNKNOWN |
| 5 | payment and event evidence persisted | `transaction_events` and `outbox_events` rows; evidence record of the original response |
| 6 | versioned telemetry published | `v11_telemetry_windows` at the end of run 4: per route 55 TRUSTED and 7 SUSPICIOUS (the no-traffic windows before warm-up); trusted windows cross-checked CONSISTENT against the switch's own measurement |
| 7 | both twins updated | the controller acknowledged 142 telemetry windows and 4,332 events (204/200), 0 refused, 0 dead letters |
| 8-10 | capacity estimation, look-ahead, budget optimisation | controller reasons in the audit: `CONTROLLER_WARMING_UP` → `NO_SAFE_BUDGET` → `SAFE_BUDGET_SELECTED` (cap from conservative headroom; `J(b)` vs `J(0)` in the PESSIMISTIC band) |
| 11 | strict v1.1 decision returned | `dec-00000037-…` (DECISION; 5 selected) |
| 12 | decision validated at execution time | gate admitted 1, refused 4 `OVER_BUDGET` |
| 13 | eligible status inquiry dispatched | v3 `CHECK_STARTED` `chk-955d9e12…`, actor DISPATCHER |
| 14 | authoritative signed status result | evidence `STATUS_CHECK_RESPONSE`, reported SUCCESS, **verified true**, key `SWITCH-v1` |
| 15 | ledger updated | v4 `PAYMENT_SUCCEEDED` (with `evidence_id` and `check_id`), v5 `PAYMENT_CLOSED` |
| 16 | Transaction Twin updated | 30 status results delivered to the controller (loop log); intake idempotent |
| 17 | audit trail and final state in the dashboard | the "AORTS v1.1 security" tab against this database (see below) |

Totals for run 4's database at export: 1,424 transactions, 4,332 transaction events, 1,424
evidence records, 134 telemetry windows (both routes), 60 decision audit rows, 8,544 ledger
entries, 0 dead letters, 0 loop step errors.
The export's per-file SHA-256 is in `export-manifest.json`. The 5 MB export itself is not
committed; it can be regenerated with `demo export`.

## Dashboard

Streamlit 1.66.1 in the `dashboard` container (http://127.0.0.1:18501), reading the control
API against run 4's database. I opened it in a browser and saved screenshots under
`docs/integration/evidence/stage11/`:

* `dashboard-overview.jpg`: 1,424 payments, all 1,424 successful, 0 ambiguous at the time, and
  the ledger-integrity check balanced.
* `dashboard-v11-decisions.jpg`, from the "AORTS v1.1 security" tab:
  * ledger integrity OK;
  * 0 duplicate check starts, 0 second reversals, 0 unverified evidence applied, unresolved
    backlog 0;
  * decision sources on A1-B: `controller` 51 ticks with 30 checks dispatched,
    `fail_closed_honoured` 9 ticks with 0.
* `dashboard-v11-telemetry-evidence.jpg`, taken after 60 s of additional honest traffic:
  * the latest 150 windows per route, with 28 and 27 TRUSTED (the rest are idle windows after
    the run, which the validator flags);
  * 1,754 verified original-payment responses, 0 unverified.

Limitation: the per-bank switch counters are kept in the switch process's memory. They were
empty after the container was recreated with the new image, because no status checks had run
since. The decisions and checks dispatched come from the database audit, which survives
restarts. The Stage 10 evidence runs (flooding 55, poisoning 95, resilience 5, replay 10) are
listed from the read-only evidence mount.

## Tests (K3)

The Docker system runs the same code as the test suite. The required behaviours are covered by
PostgreSQL-backed tests in the full suite:

* financial invariants, migration consistency and v1.1 HTTP integration;
* evidence authentication, state-version and timing-policy enforcement;
* concurrent replay protection and duplicate dispatch prevention;
* controller unavailable / fail-closed, restart with outbox redelivery;
* security regression and dashboard data.

The full suite result for this checkpoint is in the completion register. The new Stage 11
unit tests are in `tests/trackb/unit/test_stage11_worker.py`.

## Limitations

* `AORTS_ESTIMATOR_PROFILE=fast` (declared compressed time constants) is used for the
  demonstration.
* The positive-budget path needed the Stage 10 workload shape: demonstrated capacity above the
  current load, a backlog large enough to beat `min_improvement`, and no fresh lost
  confirmations inside the honest windows. With a small backlog, or with lost confirmations
  during the windows, the safe controller stays at zero (runs 2-3).
* No load or soak testing. Timings are from one laptop.
