# Recovery execution and measurement

**Owner: Owner B · closes the research chain**

Runs a controlled failure, executes a recovery strategy, and **measures what
actually happened**: RTO, RPO exposure, availability and transaction integrity.

```
telemetry → failure (I4) → recovery action → measured outcome (I6)
```

Owner A's optimizer decides *which* strategy to use and predicts its RTO/RPO.
This module executes and measures, and leaves `predicted_rto_seconds` /
`predicted_rpo_seconds` null for the optimizer to fill, so predicted-versus-actual
can be evaluated.

---

## What is measured, and how

| Metric | How it is obtained |
|---|---|
| **RTO** | Failure start → the first *sustained* recovery in the probe timeline (two consecutive healthy probes; one success between failures is a flap, not a recovery) |
| **RPO exposure** | Failure time − newest successful backup (real `aort_backup` state). No backup means **unknown**, not zero: that is unbounded exposure |
| **Availability** | Share of probes that succeeded across the incident |
| **Transaction integrity** | Ledger debits vs credits after recovery (`balanced` / `unbalanced` / `unknown`) |
| **Data preserved** | Banking row counts before and after (`m_client`, `m_savings_account`, `m_loan`, `acc_gl_journal_entry`) |

Every measurement returns `null` rather than a flattering default when the
answer is unknown. A service that never returned has **no** RTO — reporting 0
would claim instant recovery.

## Strategies

| Failure | Recovery | 
|---|---|
| `service_stop` | `restart_service` |
| `service_pause` | `unpause_service` |
| `backup_stop` | `restart_service` |

`cpu_throttle` has **no** strategy: resetting a CPU quota is not one of the
three modelled strategies, and mapping it to a restart would misreport what
recovery was performed. `restore_from_backup` exists in the contract and runs
through `aort_backup`; it is not yet wired into the experiment runner.

## Usage

```bash
# banking + telemetry stacks running; aort-injection installed
pip install -e infrastructure/injection
pip install -e infrastructure/recovery

python -m aort_recovery run --scenario service_pause --component fineract --failure-seconds 20
```

Exit codes: `0` recovered · `1` never recovered · `2` invalid parameter ·
`3` the experiment or recovery command failed.

Outcomes append to `infrastructure/recovery/runs/outcomes.jsonl` as I6 records
(see [`contracts/i6-recovery-outcome.provisional-0.1.schema.json`](../../contracts/i6-recovery-outcome.provisional-0.1.schema.json),
**provisional, not frozen**), with the matching I4 injection events alongside.

## Measured example (2026-09-23, real run)

```
experiment: service_pause on fineract, failure 20s
  recovered          : True
  RTO (actual)       : 50.4 s
  RPO exposure       : 31.9 s
  availability       : 16.67% (2/12 probes)
  ledger integrity   : balanced
  rows before/after  : m_client 9, m_savings_account 9, m_loan 14,
                       acc_gl_journal_entry 210  (unchanged)
```

**RTO exceeded the injected outage — 50 s for a 20 s pause.** Releasing the
container is not the same as being able to serve: Fineract needed roughly
another 30 s before its health endpoint reported `UP` again. That gap is
exactly the kind of thing a DR plan based on assumed RTOs would miss, and it is
only visible because recovery is measured rather than estimated.

Full records in [`evidence/`](evidence/).

## Safety

- The recovery runs in a `finally`: if probing fails, the stack is still
  restored rather than left in the injected failure state (tested).
- A target that is not already running is refused.
- Validation happens before anything is touched: a bad experiment issues no
  docker command at all.
- A failed recovery command raises loudly instead of being reported as a
  successful experiment.

## Tests

```bash
cd infrastructure/recovery
pip install -e .[test]
pytest              # measurement maths, orchestration, refusals
pytest --live       # a real pause-and-recover experiment, measured end to end
```

## Limitations

- **`restore_from_backup` is not exercised** by the runner. Restoring over the
  live tenant database would disrupt Fineract, so restore is currently verified
  against a scratch database in `banking/backup`. Measuring a real
  restore-based RTO needs a deliberate teardown/restore cycle.
- RTO resolution is bounded by `--probe-interval` (default 2 s).
- Availability is probe-based, not per-transaction: it measures the health
  endpoint, not the success rate of real banking operations.
- Single-component failures only; no compound scenarios.
- RPO is an *exposure window* (time since the last backup), not a count of
  transactions that would actually be lost.
