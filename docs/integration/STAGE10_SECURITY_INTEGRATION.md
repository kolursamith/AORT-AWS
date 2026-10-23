# Stage 10: controller security, resilience and three-attack integration

Branch `feature/aorts-integration` (pushed), on top of Stage 09R (`00c51ec`); evidence produced from commit `f068383` (source digest `d75babd216dd5f554cf89d88fb273ca46effbd14c537643f8e1960aa5653b24a`). Everything ran
in a synthetic, local, isolated environment:
- in-process Track B services (real HTTP handlers, real HMAC signing);
- a real Track A `ControllerCore` (twins, Stage 06 estimator, Stage 07 look-ahead, Stage 08
  optimizer) reached over its FastAPI app;
- a disposable PostgreSQL 16.15 container.

No real bank, NPCI/UPI or public system was contacted.

## A. Threat models

| # | threat | attacker capability | asset at risk | where it is stopped |
|---|---|---|---|---|
| 1 | status-check flooding (bank A3) | a remitter bank that is a valid switch client sends checks far above its share | switch capacity shared with honest banks and payments | switch admission before the worker pool (Track B): per-bank rate limit, route and global limits, Tier-2 credits. Integrated loop: controller budget and execution gate |
| 2 | digital-twin telemetry poisoning | the remitter-side telemetry source reports false throughput, latency, timeout rate or backlog; it replays, re-attributes or strips windows | the controller's capacity estimate, and so its check budget | Track B validator (switch cross-check, EWMA, CUSUM). Route twin (sequence, source, experiment, schema). Stage 06 estimator (trusted windows only, conservative lower bound). Execution gate and pre-dispatch caps |
| 3 | transaction replay | duplicate or stale payments, events, status results and reversal notifications; forged, expired or foreign signed answers | money (a second debit, credit or reversal), terminal-state regressions, the twin | ledger (row-locked version CAS, unique operation ids, evidence rules). Remitter `verify_response` (HMAC, key identity, ±300 s). Controller intake (idempotency, conflict detection, source and route identity) |
| 4 | controller unavailable, slow, malformed, mismatched or restarted | network faults; a buggy or partially upgraded controller | recovery safety and liveness | v1.1 client and `gate_decision` (outcomes). Track B's labelled fallback (bounded). The watermark gate. The outbox replay procedure |

The controller is trusted to compute but not to act. Track B re-gates every decision against
authoritative rows, and no controller output can authorise a financial operation.

## B. Defence implementation (Stage 10 changes)

Narrow changes, every one justified by an observed failure or an observable signal:

| change | file | why (observed) |
|---|---|---|
| validated telemetry reports the **payment path only**; the v1.1 export adds the remitter's own check completions back as a stated split | `integration/v11.py` | resilience run: during the 2-per-tick fallback, the remitter's own status-check answers inflated the throughput and timeout figures that the validator cross-checks against the switch, which counts payments only. Honest windows were flagged, and the controller stayed fail-closed after the faults cleared |
| per-experiment delivery cursor (`events:<experiment>`) | `integration/v11.py` | isolation test: a second experiment's delivery skipped the first experiment's rows (EXPERIMENT_MISMATCH) and advanced the shared cursor past them, so those events were never delivered |
| telemetry **watermark** sent with every decision request (`delivered_watermark`: the highest window sequence the controller acknowledged) | `integration/v11.py`, `dispatcher/service.py` | a restarted or lagging controller decided on older state. With the watermark it answers `TELEMETRY_STALE` (fail-closed) until it has applied what Track B knows it delivered. The controller already supported the gate; Track B never sent it |
| `Delivery.replay_events()` and `AortsLoop.restart_controller(replay_outbox=True)` | `integration/v11.py`, `integration/loop.py` | after a restart the controller had no transaction projections and selected nothing (`NO_ELIGIBLE_TRANSACTIONS`). Re-sending the outbox is the documented operator procedure; intake is idempotent. Detection is not automatic: v1.1 has no controller instance id |
| a fallback tick never carries a controller decision id; whatever the controller returned is kept as `controller_decision_id` | `dispatcher/service.py` | outage test: after a wrong-route decision was refused, the fallback jobs were tagged with the refused decision's id |
| evidence storage tolerates malformed signature headers and body fields (stored as NULL, never verified) | `remitter/service.py` | replay run: a response with duplicated signature headers crashed the evidence insert (`value too long`) |
| `check_status` reports a status only for verified evidence (`unverified_status` otherwise) | `remitter/service.py` | replay run: a forged or expired "SUCCESS" was returned as `status` and forwarded to `controller.observe`. The ledger was never affected; that defect predates 09R |
| an authorised manual reversal records the confirmed-failure evidence id | `ledger/service.py` | replay run, auto-reverse off: REVERSAL_COMPLETED was dead-lettered (EVIDENCE_REQUIRED) because the manual path recorded no evidence id |
| dispatcher v1.1 audit rows carry the experiment id | `dispatcher/service.py` | the dashboard filters by experiment |
| `AortsLoop`, `Workload`, `ControllerFaults`, `ambiguous_backlog` | `integration/loop.py` (new) | the integrated loop as one runnable unit, a steady seeded workload, controller fault injection at the transport |
| v1.1 replay cases | `integration/replay_v11.py` (new) | attacks on the evidence and delivery paths added in 09R |
| v1.1 security view: aggregation, endpoint `GET /api/v1/aorts/v11/summary`, dashboard tab | `dashboard/v11_metrics.py` (new), `control/routes.py`, `dashboard/api_client.py`, `dashboard/app.py` | dashboard integration from genuine sources |
| Stage 10 experiment runner | `experiments/stage10_security.py` (new) | conditions A–E, five seeds, manifest |

Payment value semantics, debit/credit rules, reversal authority, the safety gate and every
financial invariant are unchanged. No migration was added. Track B's validator, flooding
generator, replay simulator, rate limiter, credits and budget guard are reused unchanged.

Also changed after the interrupted first campaign (`f068383`): the experiment harness writes each run's metrics row to a file and supports `--resume <experiment_id>` (only completed runs of the same experiment, source digest and configuration whose row hash matches are reused). Rows of the interrupted campaign had been lost; see `evidence/stage10/superseded-c4bac0b7/README.md`.

## Experiment provenance (applies to C–F)

- Campaigns: replay 10 runs, resilience 5, poisoning 95, flooding 55. **All completed, 0 failed.**
- One source digest for all four (`d75babd2…`, scope: `backend/aorts`, `intelligence`, `contracts` `*.py`), commit `f068383`. Poisoning and flooding record `tracked_changes_uncommitted: true`: the only tracked change during those runs was the `pyproject.toml` dashboard pin (Streamlit 1.66.1 / Pillow 13.0.0, a dependency-audit fix); no executed source changed, as the identical digest shows.
- Real PostgreSQL 16.15 (disposable container, tmpfs). Python 3.11.15, macOS arm64. The host was kept awake (`caffeinate`) and no other workload ran during the campaigns.
- Seeds 101–105, paired across conditions within each experiment (same seed → same workload, warm-up and timeline).
- Integration profile: `FAST_ESTIMATOR` (estimator time constants compressed to seconds, declared), switch 8 core workers × 40 ms, status checks sharing the core pool (`switch_check_workers=0`), fixed 1 s check spacing, 900 s deadline.

## C. Flooding results (bank A3, Track B's generator, integrated loop running)

Mean over 5 paired seeds; attack 8 s at the stated rate on route B; legitimate A1/A2 checks 3/s per bank. "Defended" = Track B rate limiting + Tier-2 credits on; ledger protections are on in every arm.

| condition | A3 rps | legit check success during | legit check p95 ms | A3 checks reaching workers | A3 429 (all RATE_LIMITED) | detection / containment s | payment p95 B ms | controller budget A1-B during | unresolved end |
|---|---|---|---|---|---|---|---|---|---|
| normal | 0 | 1.00 | 61.8 | 0 | 0 | – | 195 | 0.27 | 65.2 |
| flood, undefended | 150 | 0.89 | 619.6 | 1200 | 0 | – | 341 | 0.18 | 68.0 |
| flood, defended | 150 | **1.00** | **60.0** | 99 | 1101 | 0.14 / 1.0 | 176 | 0.45 | 60.2 |
| healthy parallel route (A1/A2 → C), undefended | 150 | 0.85 | 630.1 | 1200 | 0 | – | 342 | 0.20 | 69.2 |
| healthy parallel route, defended | 150 | 1.00 | 58.3 | 99 | 1101 | 0.14 / 1.0 | 175 | 0.80 | 53.8 |
| flood, undefended / defended | 10 | 1.00 / 1.00 | 60.6 / 58.1 | 80 / 80 | 0 / 0 | – | 186 / 186 | 0.67 / 0.48 | 55.0 / 57.6 |
| flood, undefended / defended | 50 | 1.00 / 1.00 | 59.1 / 57.8 | 400 / 99 | 0 / 301 | – / 0.48, 1.0 | 151 / 184 | 0.85 / 0.50 | 56.6 / 58.2 |
| flood, undefended / defended | 100 | 1.00 / 1.00 | 104.9 / 56.7 | 800 / 99 | 0 / 701 | – / 0.22, 1.0 | 163 / 174 | 0.77 / 0.53 | 58.2 / 57.8 |

Paired defended − undefended at 150/s (5 pairs, bootstrap 95 % CI from Track B's helper): legitimate check success **+0.111 [0.064, 0.156]**, legitimate check p95 **−560 ms [−580, −539]**, A3 checks accepted **−950 [−956, −941]**. Every seed improved (success 0.80–0.96 → 1.00; p95 590–657 → 52–65 ms).

Findings:
- Containment is Track B's admission (per-bank token bucket): A3 is held to its quota (99 accepted in 8 s); A1/A2 keep full service. Payments themselves kept closing at the offered 3/s in every arm (payment p95 on B rose from ~176 to ~341 ms undefended).
- **Route isolation is incomplete without defences**: with legitimate traffic moved to the healthy route C, undefended legitimate checks still degraded (0.85, p95 630 ms) because checks share the switch's core worker pool (`switch_check_workers=0` in this profile). Defences restore it.
- The controller's budgets are small throughout (mean ≤ 0.85 checks/s on A1-B); the controller is not the containment mechanism for flooding, and its decisions are re-gated by Track B. Its return to a positive budget after the attack varied widely (1.0–10.2 s; some seeds no positive budget before the run ended), so no recovery-time claim is made for the controller.
- At 10/s the A3 rate is within its quota: nothing is rejected and nothing is harmed.

## D. Poisoning results

A1-B's telemetry source lies for 12 s while route B's capacity drops unannounced (1 worker, +500 ms at the beneficiary) in every arm; route C stays honest. "Unprotected" disables only Track B's telemetry validator (research arm; twin trust rules, the Stage 06 estimator, the execution gate and all ledger protections stay on). Mean over 5 seeds.

| lie | arm | windows flagged | positive-budget ticks during | max budget | max safe headroom | checks dispatched on B | C dispatched |
|---|---|---|---|---|---|---|---|
| +30 % throughput | clean / unprotected / protected | 0.88 / 0.00 / 0.88 | 1.2 / 3.0 / 1.8 | 0.6 / 0.8 / 1.0 | 3.41 / 5.20 / 5.05 | 0.6 / 0.2 / 0.8 | 5.6 / 1.6 / 6.4 |
| +100 % throughput | clean / unprotected / protected | 0.84 / 0.00 / 0.83 | 1.6 / **5.2** / 0.8 | 0.8 / 1.0 / 0.4 | 4.50 / 6.50 / 2.40 | 0.8 / 0.6 / 0.4 | 5.4 / 4.4 / 2.0 |
| falsely low latency | clean / unprotected / protected | 0.83 / 0.00 / 0.84 | 1.2 / 1.4 / 1.6 | 0.6 / 0.8 / 0.8 | 3.54 / 5.12 / 4.58 | 0.6 / 0.8 / 0.8 | 3.8 / 3.8 / 5.4 |
| zero timeouts | clean / unprotected / protected | 0.84 / 0.00 / 0.84 | 1.4 / 2.0 / 2.0 | 0.8 / 1.0 / 1.0 | 3.97 / 6.35 / 5.61 | 0.6 / 1.0 / 1.0 | 4.0 / 6.0 / 6.2 |
| inconsistent backlog | clean / unprotected / protected | 0.84 / 0.00 / 0.84 | 2.0 / 1.4 / 1.8 | 1.0 / 1.0 / 1.0 | 5.59 / 5.93 / 5.16 | 1.0 / 1.0 / 0.8 | 6.2 / 6.8 / 5.6 |
| conflicting measurements | clean / unprotected / protected | 0.83 / 0.00 / 0.84 | 1.2 / 1.0 / 1.2 | 0.8 / 1.0 / 0.6 | 4.21 / 5.52 / 3.52 | 0.6 / 1.0 / 0.6 | 3.4 / 6.2 / 5.0 |

Protocol-level poisoning through the controller's real intake (5 seeds, every case refused, 0 applied, twin unchanged): replayed signed observation and stale sequence → `DUPLICATE_SEQUENCE`; wrong or unregistered source → `UNKNOWN_SOURCE`; other experiment and missing observation window → `SCHEMA_INVALID`. Source silent for 10 s: the controller answered `TELEMETRY_STALE`, the fallback dispatched 0 (one positive-budget tick at the start of the silence, before staleness was reached); after observations returned, the first positive budget came 2.2 s later in every seed.

Findings and limitations (not hidden):
- **The validator's flags are not specific to lying.** It flagged ~84 % of A1-B windows in the *clean* arm too, because the unannounced capacity drop itself makes the remitter's report deviate from the switch's measurement. Detection here means "this window is not trusted", which is the safe reaction to either cause.
- With the validator disabled, +100 % inflation raised the number of positive-budget ticks during the attack from 1.6 (clean) to 5.2, and the estimator's maximum headroom from 4.5 to 6.5 checks/s: the residual Stage 06 exposure (detection lag after a drop, inflated internal estimates). The optimizer and the execution gate kept actual budgets ≤ 1 check/s and dispatches ≤ 1 check per 12 s attack in every arm; no financial effect was possible in any arm.
- Missing-window detection is enforced by the contract (a window without its bounds is schema-invalid), not by inference.
- Not every reachable warm-up reached a positive decision before the attack (`warm_ready` 3–5 of 5 seeds per arm, recorded per run).

## E. Replay results (PostgreSQL)

10 runs (5 seeds × auto-reverse on/off). Track B's replay simulator: 23 cases per run, **0 duplicate reversal postings**. v1.1 replay cases: 13 per run, **0 unexpected** in all 10 runs:

| v1.1 case | observed (every run) |
|---|---|
| duplicate payment (same idempotency key) | same transaction, one DEBIT |
| duplicate event delivery (cursor reset) | 0 newly applied; unrepresentable rows dead-lettered again; twin unchanged |
| duplicate status result | 200 `DUPLICATE_EVENT`, twin unchanged |
| concurrent duplicate result (8 parallel) | all 200, none applied twice |
| wrong transaction id | 404 `UNKNOWN_TRANSACTION` |
| wrong route | 409 `ROUTE_IDENTITY_MISMATCH` |
| conflicting authoritative outcome | 409, twin keeps SUCCESS |
| old transaction version | identical → duplicate (200); altered → refused (422) |
| unregistered event source | 403 `UNKNOWN_SOURCE` |
| invalid source signature | evidence stored unverified, no settlement, still AMBIGUOUS |
| expired evidence | rejected, no settlement |
| answer for another transaction | discarded (`WRONG_TRANSACTION`), target still AMBIGUOUS |
| duplicate reversal completion | 200 duplicate, one REVERSAL posting |

Ledger checks after every run: integrity OK, 0 duplicate check starts, 0 second debits, 0 second reversals, 0 unverified evidence applied.

## F. Controller outage and resilience

5 seeds, 18 consecutive 6 s phases on the running loop (routes A1-B and A1-C). Every seed:

| phase | outcome seen by Track B | source of checks | checks |
|---|---|---|---|
| baseline | DECISION | controller | 0–9 |
| controller down, HTTP 500, timeout (3 s > 2 s client timeout), malformed response, wrong protocol version, wrong route id | CONTROLLER_UNAVAILABLE / TIMEOUT / MALFORMED_RESPONSE / IDENTITY_MISMATCH | `trackb_fallback` (no controller decision id) | ≤ 2 per tick per route (cap 2) |
| slow but within timeout (0.5 s) | DECISION | controller | 0–8 |
| recovered after faults | DECISION | controller | 0–8 |
| explicit FAIL_CLOSED latch | EXPLICIT_FAIL_CLOSED (`TELEMETRY_UNTRUSTED`) | `fail_closed_honoured` | **0** |
| latch released, then poisoned telemetry (+100 %), source disconnected, observations return | WARMING_UP (`CONTROLLER_WARMING_UP`) | `fail_closed_honoured` | 0 |
| event-delivery backlog (watermark gate) / drained | WARMING_UP or DECISION `NO_SAFE_BUDGET` | controller / fail-closed | 0 |
| controller restart (outbox replayed) / after restart | WARMING_UP | `fail_closed_honoured` | 0 |

Totals over all runs: maximum fallback per tick 2 = cap; **checks dispatched under FAIL_CLOSED/WARMING_UP: 0; fallback during fail-closed phases: 0**. Ledger checks: all clean.

How decisions become safe again: fallback-permitted faults end at the next tick when a valid DECISION returns (seen in `recovered_after_faults`). After an explicit FAIL_CLOSED is released, after a restart, or after untrusted/missing telemetry, the estimator re-initialises and needs fresh trusted windows (`min_usable_windows`, `sustain_s`) before any positive budget; during that time Track B honours WARMING_UP with zero checks, never the fallback. **Negative finding:** in these short phases (6 s) the controller did not return to a positive budget within the remaining phases after the latch release or the restart; trust restoration takes longer than the phase length used here (Section D shows 2.2 s to a positive budget after a 10 s silence when the estimator was not re-initialised).

## G. Dashboard outputs

The "AORTS v1.1 security" tab and `GET /api/v1/aorts/v11/summary` aggregate genuine sources only: decision ticks by source (controller / `trackb_fallback` / `fail_closed_honoured`) and checks dispatched, telemetry verdicts per source, per-bank switch check counters with rejection reasons, rejected checks by code, replay/authentication rejections, evidence records and payment latency, unresolved backlog and in-flight checks, and the financial-safety counters; plus the newest Stage 10 runs CSVs. Verified by `test_dashboard_metrics_match_the_recorded_sources` and `test_dashboard_endpoint_serves_the_same_view` (PostgreSQL). The UI itself (Streamlit 1.66.1) is started and screenshotted against the live Docker system in Stage 11 (`STAGE11_DOCKER_SYSTEM.md`).

## H. Actual DB test results

Full suite on PostgreSQL 16.15 with `AORTS_REQUIRE_DB=1` (DB tests fail instead of skipping): see I. Stage 10 module `tests/trackb/integration/test_stage10_security.py`: 20 tests (flooding containment and quotas, fairness, parallel route, credits, inflated/deceptive telemetry, stale/duplicate/wrong-source/other-experiment telemetry, payment-path-only telemetry, v1.1 replay cases, concurrent replay, reversal evidence, malformed signature headers, outage fallback labelling, explicit fail-closed, trust restoration, restart with outbox replay, delivery backlog, dashboard metrics and endpoint, source/experiment isolation).

## I. Full regression

`pytest tests` on the committed Stage 10 source (`f068383` + docs/evidence) with PostgreSQL 16.15 and `AORTS_REQUIRE_DB=1`: **1644 passed, 0 failed, 0 skipped** (JUnit report checked for skips), 934 s. Ruff check and format, `compileall`, contract schema regeneration diff, `git diff --check` and gitleaks were run at the checkpoint.

## J. File changes (Stage 10)

Source: `backend/aorts/banking/{dispatcher,ledger,remitter}/service.py`, `backend/aorts/control/routes.py`, `backend/aorts/dashboard/{api_client.py,app.py,v11_metrics.py}`, `backend/aorts/integration/{v11.py,loop.py,replay_v11.py}`, `backend/aorts/experiments/stage10_security.py`. Tests: `tests/trackb/integration/test_stage10_security.py`, `tests/trackb/integration/test_v11_cross_track.py` (one line), `tests/trackb/unit/test_stage10_campaign.py`. Config: `pyproject.toml` (dashboard pins). Docs and evidence: this file and `docs/integration/evidence/stage10/`.

## K. Commits

`e0c7020` (WIP recovery checkpoint of the interrupted session's source), `f068383` (resumable harness; evidence source), and the Stage 10 checkpoint commit `feat(security): verify integrated AORTS defenses and resilience` on `feature/aorts-integration`.

## L. Evidence manifests

| experiment | experiment id | file | SHA-256 |
|---|---|---|---|
| replay | `secpkg-stage10-replay-20261023T153558Z` | runs CSV | `03fbfd9814013dd91dc37cae7a9181203984fcdcc2ed96ffae5be0d39d0e8552` |
| | | manifest | `6382633f4aa2e64f211f678653f73850362c130fb5c08b44dca557f703db775d` |
| resilience | `secpkg-stage10-resilience-20261023T153629Z` | runs CSV | `51e994a6d0d4160f58e17ca5798402bdbf5b491fe3ffc0b510a0de7483b70cd8` |
| | | manifest | `301b87462933c9cf5c619f246ac81d386bd7d576913f93510fd313469054737f` |
| poisoning | `secpkg-stage10-poisoning-20261023T155212Z` | runs CSV | `0a76fe6bfe606394e686eb2f2cfbc49ca6929a8064763a8dd4a82e4930482b75` |
| | | manifest | `575ce01c6cfeb2529bf8bbb5021345db40c4fa034c2c657c3f916ffdcd984eac` |
| flooding | `secpkg-stage10-flooding-20261023T172657Z` | runs CSV | `7828058956120fa640381fd28ef74fe265c3885aa33ac7916710f6e68db968ac` |
| | | manifest | `df6f2c684b66fd8bcfa85053a46114a0800983f080977bf45c0c72bbc421685b` |

Every manifest line has the run's seed, condition, configuration, git commit, source digest, start/end time, duration, status and the SHA-256 of each raw output (per-tick JSON, request and payment CSVs, row files in `results/processed/stage10/`, not in Git). A checksummed copy of the evidence and raw files (542 files) is in `CAD_project/AORTS_research_evidence_backup/stage10_final_2026-10-23/`.

## M. Stage 11 readiness

Stage 11 needs: the integrated loop as services between containers (`backend/aorts/integration/worker.py`), a controller container configured from the same Track B settings, and the dashboard against live data. All Stage 10 security behaviour is in the code those services run.
