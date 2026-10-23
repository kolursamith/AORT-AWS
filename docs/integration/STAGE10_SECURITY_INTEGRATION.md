# Stage 10: controller security, resilience and three-attack integration

Branch `integration/stage09-trackb` (local only), on top of Stage 09R (`00c51ec`). Everything ran
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

_Sections C–M follow from the experiment results._
