# Stage 12 findings

Every number below is taken from `RESULTS.md`, `summary.json` and `runs_flat.csv`, which are
generated from `results/processed/stage12/runs.jsonl`:

* 2,100 runs, all completed, 0 failed;
* source digest `fef8b37a…` at commit `9106899`;
* design digest `81327546…`.

Values are means over paired seeds (30 for key scenarios, 10 for the others). Brackets are 95 %
bootstrap intervals of the mean paired difference against `no-check` on the identical
workload. The independent DES was used for every arm. This is synthetic evidence at 1/50
scale. It is not a statistical-significance claim.

## Answer to the key question

> Does feedback-aware recovery scheduling reduce newly created payment ambiguity while
> preserving meaningful recovery outcomes?

**Partly; the recovery half fails.**

1. **AORTS never induced additional ambiguity.** In every one of the 13 scenarios, full AORTS
   had a paired difference in new ambiguous payments against `no-check` of +0.0. The one
   exception is the outage scenario, where Track B's fallback acts: +2.8 [0.0, 8.4]. The
   baselines did harm in degraded routes:

   | scenario | static polling: new ambiguous | static polling: backlog at end | AIMD: new ambiguous | AIMD: backlog at end |
   |---|---|---|---|---|
   | `prolonged_fault` (S2) | +6,156 [5,722, 6,592] | +1,721 | +229 [212, 248] | +651 |
   | `repay_feedback` | +254 | +862 | +193 | +653 |
   | `sudden_degradation` | +41 | +780 | +26 | +499 |

   In these three scenarios, checks could not resolve anything (resolved by check = 0 for
   every arm), so the best possible policy was not to check. AORTS matched `no-check`
   exactly. It also kept `no-check`'s time to recovery after the fault: 67.3 s in
   `prolonged_fault`, against 88.0 s for AIMD.
2. **AORTS did not preserve meaningful recovery where checks help.**

   | scenario | AORTS resolved by check | AIMD resolved by check | AIMD new ambiguous | AIMD backlog at end |
   |---|---|---|---|---|
   | `lost_confirmation` (RV-1) | 2.3 [0.6, 4.7] | 2,278 [2,250, 2,306] | −0.8 [−1.2, −0.5] | −2,279 |
   | `status_check_bursts` | 0.0 | 3,921 | +27.5 [−3.7, +89.1] | −3,924 |

   AIMD dominated AORTS in both. Fixed-budget, static polling and backoff resolved some
   payments but induced 3,000–11,600 extra ambiguous payments. **This replicates the
   preliminary negative finding with 30 paired seeds** (preliminary single seed: AORTS 0,
   AIMD 2,352, no-uncertainty 379).

## Ablations: what actually binds

| arm (`lost_confirmation`, 30 seeds) | resolved by check | new ambiguous vs `no-check` |
|---|---|---|
| full AORTS | 2.3 | +0.0 |
| no feedback term | 2.3 | +0.0 |
| no look-ahead | 2.3 | +0.0 |
| no deadline ranking | 2.3 | +0.0 |
| **no uncertainty bound** (central capacity; research only) | **475.8** [438, 517] | +0.0 |
| fixed budget through AORTS | 819.0 | +7,989 |
| `ambiguity_averse` weights | 0.0 | +0.0 |
| `recovery_seeking` weights | 2.3 | +0.0 |

* The **conservative uncertainty bound is the binding constraint**: Stage 06 headroom is 0
  on a route with lost confirmations. With the bound removed, AORTS recovered 476 payments
  per run without any extra ambiguity, still about a fifth of AIMD's recovery.
* The **feedback term and the look-ahead made no measurable difference** in any key
  scenario. They never become active because the budget cap is already 0. In
  `prolonged_fault`, no-look-ahead released 726 checks (cap = headroom) against 5 for full
  AORTS, with no change in harm or recovery. The feedback-aware mechanism therefore received
  no test of its own value in these designs. The novelty claim about it is **not supported
  by this evidence**.
* The objective weights barely matter, again because the cap is 0. The exception is
  `ambiguity_averse`, which suppressed even the 2.3 checks.

## Telemetry impairments (AORTS arms only)

* **Poisoned throughput, ×2 and ×1.3 during the S2 fault (unflagged):** AORTS released 0
  checks and caused no harm. The estimator gave no headroom on the degraded route despite the
  inflated reports.
* **Stale telemetry (300 s silence in RV-1):** AORTS released 1.2 checks against 2.3 when
  telemetry was healthy. It failed closed.
* **Controller outage (300 s in RV-1):** Track B's bounded fallback (2 per route per decision)
  resolved **542** payments, against AORTS's own 2.3 when the controller was available. The
  outage increased p95 payment latency from 0.92 s to 7.03 s. This is a further negative
  finding: a simple bounded fallback outperformed the controller's recovery on this design.

## Metrics that could not be measured here

* **Deadline completion:** every run's deadline (60 min after sending) falls beyond the
  horizon (25 or 40 min), so all payments are deadline-censored. For example, 4,168 are
  censored in one `lost_confirmation` run. The deadline ranking ablation is therefore
  untested in effect.
* **Bank fairness (Jain index):** undefined, because every design has a single remitter bank.
  Bank fairness under attack is measured in Stage 10 (flooding: legitimate A1/A2 service with
  and without defences).
* **Financial safety violations:** 0 invalid and 0 noncompliant releases in all 2,100 runs. The
  simulator has no ledger; ledger safety is measured on PostgreSQL in Stages 10-11.

## Model validity

The Stage 04S selector would have allowed the fluid model for the `lost_confirmation`,
`normal_load`, `near_saturation`, `route_recovery`, `status_check_bursts`, `stale_telemetry`
and `controller_outage` arms of AORTS. It required the DES for the S2-type designs. The DES was
used everywhere, and each run records the selector's verdict.

## Threats to validity

* All designs have one or two routes, synthetic workloads and 1/50 scale.
* The AORTS arm uses the decision pipeline (`decide`), not the HTTP service. The service is
  exercised in Stages 10-11.
* The 1 s decision interval releases at most `budget_rps` checks per decision.
* The look-ahead sees only remitter-observable inputs, as designed.
* These results do not show that AORTS is worse in every respect. They show that, as
  configured, it trades away nearly all recovery for zero added harm. AIMD achieved both on
  RV-1, but harmed degraded routes.

## Evidence files

| file | SHA-256 |
|---|---|
| `results/processed/stage12/runs.jsonl` (144 MB, one full RunRecord per run; not in Git) | `d8c1378de00d6fecdd166a24f78837447076d431671c14f4522db095bd12aac2` |
| `docs/research/stage12/manifest.jsonl` (per-run status, runtime, result hash, line hash) | `cf0964f5296527120586adb0a4a10b55d6d274e13152f12aa288deab73586929` |

A checksummed copy of the raw records and of this report is in
`CAD_project/AORTS_research_evidence_backup/stage12_2026-10-24/`. To regenerate the report:
`python -m intelligence.experiments.stage12_evaluation analyse`. To regenerate the runs:
`python -m intelligence.experiments.stage12_evaluation run --workers 8` (about 2 h on a
10-core laptop).
