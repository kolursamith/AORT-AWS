# Stage 12 evaluation methodology

The design is frozen in `intelligence/experiments/stage12_evaluation.py` (`DESIGN`). Its
SHA-256 (`design_sha256`) and the source digest of every Track A and contract Python file
(`source_sha256`) are written on every run line and manifest line. The analysis refuses to mix
digests: rows from other digests are counted and excluded.

## Question

Does feedback-aware recovery scheduling reduce newly created payment ambiguity while preserving
meaningful recovery outcomes, compared with simple status-check policies?

Both sides of the trade-off are measured:

* **harm**: new ambiguous payments induced by checks (paired difference against `no-check` on
  the identical workload), payment p95/p99 latency, and the unresolved backlog;
* **benefit**: authoritative resolutions by check, deadline completion and time to recovery.

A controller is never judged only by how many checks it suppresses. `lost_confirmation` (RV-1)
and `status_check_bursts` are recovery-valuable designs: confirmations are lost while the
payments themselves were committed, so a status check can resolve them.

## Simulator and model validity

* Engine: the **independent SimPy discrete-event simulation** (Stage 04S `des_validate`) for
  every arm. The Stage 04S selector's verdict for each arm is recorded (`selector_model`). The
  DES is used even where the fluid model would be permitted, because the AORTS arms need the
  DES's causal remitter observations, and because one engine per comparison avoids a model
  confound. S2-type (stale-drop, check-burst and feedback) designs therefore use the DES, as
  Stage 04S requires.
* Stage 12 adds two observation fields to the DES (`payments_sent`, `payment_responses`): the
  remitter's own sends and received responses, causally, per bin. A test asserts that the
  closed-loop simulation digest equals the open-loop one, so the observation logs do not change
  the dynamics.
* Scale: 1/50 of the report's reference rates (`SCALE = 0.02`). Load ratios are preserved.

## Truth separation

Policies receive only a `PolicyInput` (the remitter's outstanding and eligible ambiguous
payments, its own check feedback) and, for AORTS, the remitter-observable DES fields above.
They receive no capacity, fault schedule, beneficiary decision, customer intent or future
outcome. The harness reads ground truth only after the run, to compute metrics. A shared gate
applies the research timing policy (first check after 90 s, 90 s spacing, at most 3 counted
checks, 60 min deadline) and 10 checks per bank per decision to every STRICT arm.

## Arms

| arm | what it is |
|---|---|
| `no-check` | reference: never checks |
| `static-polling` | fixed schedule after ambiguity |
| `exp-backoff-jitter` | exponential backoff with deterministic jitter |
| `fixed-budget` | 3 checks per route per decision, oldest first |
| `aimd` | additive-increase / multiplicative-decrease on check feedback |
| `aorts` | full AORTS: Route Twin, Stage 06 estimator, Stage 07 look-ahead, Stage 08 optimizer and ranker (`decide`), releasing at most `budget_rps` checks per 1 s decision |
| `aorts-no-feedback` | ablation: forecast without the check → timeout feedback |
| `aorts-no-lookahead` | ablation: budget = safe headroom cap, no forecast |
| `aorts-no-uncertainty` | research-only ablation: central capacity instead of the conservative lower bound (backend safety unchanged; there is no ledger in the simulator) |
| `aorts-no-deadline` | ablation: age-order ranking |
| `aorts-fixed` | research reference: fixed 3/s budget through the AORTS pipeline |
| `aorts@ambiguity_averse`, `aorts@recovery_seeking` | objective weight sensitivity |

Track B's heuristic controller is **not comparable** in this harness: it needs switch-side
inputs that remitter-own telemetry does not contain (`baselines.NOT_COMPARABLE`).

## Scenarios

The thirteen scenario definitions are in `DESIGN["scenarios"]`. Telemetry impairments (stale
or poisoned telemetry, controller outage) change only what an AORTS arm observes. Baselines read
no telemetry, so for them these scenarios equal their base design. During an outage the AORTS
arm uses Track B's bounded fallback: 2 per route per decision, oldest first.

## Seeds and statistics

* Key scenarios (`lost_confirmation`, `prolonged_fault`, `repay_feedback`,
  `sudden_degradation`): 30 paired seeds (12001-12030), every arm.
* Other scenarios: 10 paired seeds (12001-12010), baselines and full AORTS.
* For each metric: mean, median and sample SD per arm. Paired differences on the identical
  workload use mean, median, counts of positive/negative/zero, and a 95 % percentile-bootstrap
  interval of the mean difference (10 000 resamples, fixed seed). The interval is reported only
  when n ≥ 10 and the differences are not all equal. No p-values or significance claims are
  made.
* Failed runs are kept in the manifest with their error. Deadline-censored payments (deadline
  beyond the horizon) are counted separately (`deadline_censored`).

## Reproduce

```bash
pip install -e '.[dev,research]'
python -m intelligence.experiments.stage12_evaluation run --workers 8
python -m intelligence.experiments.stage12_evaluation analyse
```

`run` is resumable: it skips runs already recorded for the same source and design digests.
