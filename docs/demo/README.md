# AORTS security demonstrations (Stage 13)

Three teacher demonstrations on the integrated system: bank A3 status-check flooding,
digital-twin telemetry poisoning, and transaction replay. Everything is a synthetic mock
environment, with no real bank, NPCI/UPI endpoint or customer data.

Each demonstration is a **real run**. Track B's services run in process with real HTTP
handlers and HMAC signing, against real PostgreSQL, and the real Track A controller makes the
decisions. The per-run functions are the same ones that produced the Stage 10 evidence.
Generated pages and figures are drawn only from the recorded ticks, requests and ledger
checks. The five-condition, five-seed comparisons are **quoted from the Stage 10 evidence
package** (`docs/integration/evidence/stage10`), not re-run, and each page names its source
file.

## Prerequisites

* Python environment: `pip install -e '.[dev,research,dashboard]'`.
* A disposable PostgreSQL 16, for example
  `docker run -d --name aorts-demo-pg -e POSTGRES_USER=aorts -e POSTGRES_PASSWORD=<pw>
  -e POSTGRES_DB=aorts -p 127.0.0.1:55450:5432 --tmpfs /var/lib/postgresql/data
  postgres:16-alpine`.
* `export AORTS_DATABASE_URL=postgresql+psycopg://aorts:<pw>@127.0.0.1:55450/aorts`.
* Keep the machine awake during a run (`caffeinate -i` on macOS). The loop is wall-clock
  driven, and a sleep invalidates the timing.

## Run

```bash
python -m backend.aorts.experiments.stage13_demo flooding  --seed 101   # about 3 min
python -m backend.aorts.experiments.stage13_demo poisoning --seed 101   # about 3 min
python -m backend.aorts.experiments.stage13_demo replay    --seed 101   # under 1 min
```

Each command writes `docs/demo/<attack>/README.md`, its run JSON and SVG timelines. Raw ticks go
to `results/processed/stage13/`.

## Presenting (about 15 minutes)

1. **Set the scene (2 min).** Show `docs/final/ARCHITECTURE.md` §1-2. Make three points:
   UNKNOWN is not failure; checks share capacity with payments; the controller decides budgets
   but cannot move money.
2. **Flooding (4 min).** Open `flooding/README.md`. Compare legitimate A1/A2 check success and
   latency with defences OFF and ON. Point to the A3 429 rejection reasons (rate limit,
   credits) and the controller's budget on route B in the timeline. Then show the paired-seed
   table, including the parallel-route conditions.
3. **Poisoning (4 min).** Open `poisoning/README.md`. Route B loses capacity while its source
   reports twice its throughput. Compare the detection rate, the maximum budget and the checks
   dispatched across clean, unprotected and protected. Name the residual risk: the estimator's
   detection lag and a consistent liar (see limitations).
4. **Replay (3 min).** Open `replay/README.md`. Walk through the v1.1 case table: one debit,
   duplicates acknowledged without effect, forged or expired evidence stored but never
   applied, one reversal posting.
5. **Live system (5 min).** Start the stack (`infra/aorts/README.md`) and open the **AORTS
   Command Center** at http://127.0.0.1:18501:
   * *Demo control*: create lost-confirmation payments, start honest traffic.
   * *Intelligence engine*: the live decision and its reason.
   * *Security operations*: the recorded 5-condition comparisons and the live counters.
   * *Ledger & audit*: the financial checks.

   Screenshots from the verified run are in `docs/demo/dashboard/`. The full procedure is
   `docs/final/DEMO.md`.

## Expected versus verified

The pages show what each run actually measured. Before a run, the expected observations
are:

* flooding: A3 is mostly rejected with defences on, and legitimate check success during
  the attack is at least as good as with defences off;
* poisoning: protected windows are flagged, and no positive budget follows from a flagged
  window;
* replay: zero unexpected cases, and no second financial effect.

If a run does not show an expected observation, the page shows that instead. Nothing is
edited by hand.
