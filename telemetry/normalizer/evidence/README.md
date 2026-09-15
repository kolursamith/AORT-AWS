# Layer 1a evidence — real I1 output from the running stack

Both files were produced by `python -m aort_normalizer` against the live
Phase 2 + Phase 3 stacks on 2026-09-15 (clean slate: both stacks torn down with
`docker compose down -v` and rebuilt that day). Every value comes from a real
Prometheus query. Each line validates against
[`contracts/i1-observation.provisional-0.1.schema.json`](../../../contracts/i1-observation.provisional-0.1.schema.json).

## `snapshot-during-phase2-regression.jsonl`

One snapshot, 27 records, taken at 17:05:24Z **while the Phase 2 regression
(`banking/verify/verify_workload.py`) was driving Fineract traffic**, shortly
after the live integration test's OTel-instrumented workload.

- `quality`: 27 × `ok`
- 27 = 22 ungrouped signals + `operation_rate` split into 5 banking categories

## `active-to-idle-transition.jsonl`

Six snapshots, 10 s apart (17:06:49Z–17:07:39Z), 168 records. Intended to
capture the Phase 3 regression's workload, but a 20 s start delay meant it
caught **only the end of that workload and the transition to idle**. It is kept
because that transition is the useful part:

| Snapshot | Fineract req/s | Operations/s by category | Non-`ok` signals |
|---|---|---|---|
| 1 · 17:06:49Z | 1.08 | client 0.16, loan 0.08, savings 0.04 | none (28 × `ok`) |
| 2–6 | 0.0 | all 0.0 | 3 × `non_finite` |

The three `non_finite` records in snapshots 2–6 are exactly the signals defined
as a ratio of rates — `fineract.http_latency_mean_seconds`,
`banking-operations.operation_error_ratio` and
`banking-operations.operation_latency_mean_seconds`. With no traffic in the
1-minute window they are 0/0, so they carry `value: null` instead of a made-up
number.

Also visible:

- Operation rates read a true `0.0` (`ok`), not `missing`: the counter series
  still exist (the collector keeps them for 10 minutes), they just stopped
  increasing. They become `missing` once they expire.
- PostgreSQL commits fall from ~40/s at the end of the workload to a ~4/s
  background baseline (exporter queries plus Fineract's own scheduler).

## What this evidence does not show

- A steady workload observed over a long period. The live integration test
  covers the active case, asserting every signal is `ok` under steady traffic.
- Anything about backup or replication state — not yet a signal (Layer 1b).
