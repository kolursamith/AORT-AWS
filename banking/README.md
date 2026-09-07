# AORT — Banking Foundation (Phase 2)

Reproducible local deployment of **Apache Fineract + PostgreSQL**, plus a Python
workload generator that drives it through its REST APIs to produce representative
core-banking activity.

This is the Phase 2 deliverable of the AORT project (BCSE355L — Cloud Architecture
Design). It supplies the observable banking workload that the later phases —
telemetry validation, the operational digital twin, failure injection, prediction
and recovery optimization — are built on.

> **Scope statement.** This is a controlled academic prototype, not a production
> banking platform, and Apache Fineract is used as a *representative* core-banking
> workload. It is not a complete bank. See [Limitations](#limitations-and-assumptions).

---

## Contents

| Path | Purpose |
|---|---|
| `docker-compose.yml` | The Fineract + PostgreSQL stack |
| `.env.example` | Configuration template (copy to `.env`) |
| `docker/postgres/initdb/` | Creates Fineract's two databases on first boot |
| `workload/aort_workload/` | The workload generator package |
| `workload/runs/` | Per-run records written by the generator (git-ignored) |
| `verify/verify_workload.py` | Verification that the workload really runs |

---

## Prerequisites

- Docker Engine with Compose v2 (developed against Docker 29.6, Compose v5.2)
- Python 3.11+
- Roughly 2 GB free RAM for the Fineract container and 1 GB disk for images

---

## Starting the stack

```bash
cd banking

# 1. Create your local configuration (one time)
cp .env.example .env

# 2. Start Fineract and PostgreSQL
docker compose up -d

# 3. Watch until Fineract reports healthy (first boot runs Liquibase migrations)
docker compose ps
```

First boot takes roughly **60–90 seconds** while Fineract creates its schema.
The stack is ready when `docker compose ps` shows both containers as `healthy`.

Check it directly:

```bash
curl http://localhost:8080/fineract-provider/actuator/health
# {"status":"UP","groups":["liveness","readiness"]}
```

| Endpoint | URL |
|---|---|
| Fineract REST API | `http://localhost:8080/fineract-provider/api/v1` |
| Health | `http://localhost:8080/fineract-provider/actuator/health` |
| Prometheus metrics | `http://localhost:8080/fineract-provider/actuator/prometheus` |
| PostgreSQL | `localhost:5432` (databases `fineract_tenants`, `fineract_default`) |

Default Fineract credentials are `mifos` / `password` with tenant `default` —
Fineract's own seeded superuser, unchanged.

### Stopping and tearing down

```bash
docker compose stop          # pause, keep all data
docker compose down          # remove containers, keep the database volume
docker compose down -v       # remove containers AND all banking data (full reset)
```

`down -v` is what you want to reproduce a run from scratch: it drops the
`aort_pgdata` volume, so the next `up -d` re-provisions the databases and
re-runs Fineract's migrations.

---

## Running the workload generator

```bash
cd banking/workload

# one time
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
# source .venv/bin/activate && pip install -r requirements.txt  # Linux/macOS

# run it
./.venv/Scripts/python.exe -m aort_workload --clients 5 --iterations 40 --seed 42
```

| Option | Default | Meaning |
|---|---|---|
| `--clients N` | 5 | New clients onboarded this run |
| `--iterations N` | 40 | Operations in the randomised activity loop |
| `--loans N` | 2 | Loans originated during onboarding |
| `--seed S` | none | Fixes the RNG so a run replays the same decisions |
| `--delay S` | 0 | Pause between operations, to stretch a run over time |
| `--business-date` | today | Date used for banking operations (`YYYY-MM-DD`) |
| `--output-dir` | `workload/runs/` | Where run records are written |
| `--no-output` | off | Print the summary without writing files |

The generator exits **non-zero if any Fineract call failed**, so it can gate CI.

It is safe to run repeatedly. Configuration (chart of accounts, products,
payment types) is created once and reused; each run adds new clients and
activity on top of whatever is already there.

### What it actually does

**Setup (idempotent, once per database):**
- enables the working currency (INR)
- creates a 14-account chart of accounts (assets, liabilities, income, expense)
- creates three payment types (cash, internal transfer, cheque)
- creates a savings product and a loan product, both with **cash-based accounting**

**Onboarding, per client:**
- creates and activates a client
- opens a savings account: submit → approve → activate
- posts an opening deposit
- for a subset: applies for, approves and disburses a loan

**Activity loop** — a weighted random mix, so load resembles ongoing branch
activity rather than one scripted sequence:

| Operation | Weight | Category |
|---|---|---|
| `savings.deposit` | 28 | Transaction |
| `savings.withdrawal` | 14 | Transaction |
| `loan.repayment` | 14 | Transaction |
| `savings.read` | 10 | Accounts |
| `client.read` | 8 | Customer |
| `client.list` | 6 | Customer |
| `accounting.journalentries.list` | 6 | General ledger |
| `accounting.journalentry.create` | 5 | General ledger |
| `accounting.journalentries.by_account` | 4 | General ledger |
| `loan.originate` | 5 | Loans |

Withdrawals read the account's real balance from Fineract first, so the amount
requested is always one the account can cover.

### Coverage against the Phase 2 scope

| Required scope | How it is covered |
|---|---|
| Customer/client operations | Client creation, activation, reads, paged listing |
| Accounts and savings/deposits | Savings account lifecycle + deposits |
| Loan operations | Application, approval, disbursement, repayment |
| Transactions | Savings deposits/withdrawals, loan repayments, all with payment types |
| Accounting / general ledger | Automatic double-entry postings from cash-based products, plus manual journal entries and ledger queries |
| REST API activity | Every operation is a real Fineract REST call; reads and writes mixed |
| Database workload | All of the above lands in PostgreSQL through Fineract |
| Application/infra health | Actuator health and Prometheus endpoints exercised by verification |

**On general-ledger behaviour:** both products use cash-based accounting, so
Fineract itself posts balanced double-entry journal entries for every deposit,
withdrawal, disbursement and repayment. The ledger activity is produced by
Fineract's own accounting engine — the generator does not write journal rows to
make the ledger look populated.

### Run records

Each run writes two files to `workload/runs/`:

- `<run-id>.operations.jsonl` — one record per Fineract call: operation, HTTP
  method and path, status code, success flag, latency in milliseconds, the
  resource id returned, and any error message Fineract gave.
- `<run-id>.summary.json` — per-operation attempt/success/failure counts and
  latency min/median/max.

Every field comes from an actual HTTP response. Nothing is synthesised.

> ⚠️ **This record schema is provisional** (`"schema_version": "provisional-0.1"`).
> It exists to serve Phase 2 and to give the telemetry and dataset phases
> something concrete to react to. Freezing it is a joint Owner A / Owner B
> decision under the project's contract rules — it has **not** been agreed as a
> shared interface, and should not be treated as one yet.

---

## Verification

```bash
cd banking
./workload/.venv/Scripts/python.exe verify/verify_workload.py
```

Runs 19 checks against the live stack and exits non-zero if any fail:

- Fineract health reports `UP`; the authenticated API answers
- the Prometheus metrics endpoint is exposed
- bootstrap resolves products, GL accounts and payment types
- clients, savings accounts and loans are created
- all five in-scope banking categories are exercised
- each key operation succeeds at least once
- every Fineract call in the run succeeded
- **independent confirmation:** client and journal-entry counts are re-read from
  Fineract before and after the run and must actually have increased

That last group matters — it confirms the activity from Fineract's own state
rather than trusting the generator's self-report.

### Verified result

Run from a genuine clean slate on 2026-09-07 — `docker compose down -v`
(destroying the database volume), then `up -d`, then verification — against
`apache/fineract:1.12.1` and `postgres:16.1` on Docker 29.6 / Compose v5.2:

```
Stack availability
  [PASS] Authenticated Fineract API responds - ready in 51.5s
  [PASS] Fineract actuator health reports UP - status=UP
  [PASS] Prometheus metrics endpoint is exposed - HTTP 200, 98703 bytes

Baseline
  [PASS] Baseline counts readable - clients=0, journal_entries=0
...
Call outcomes
  [PASS] Every Fineract call succeeded - 85/85 succeeded

Independent confirmation (re-read from Fineract)
  [PASS] Client count increased by the number onboarded - 0 -> 3
  [PASS] Fineract posted new journal entries for this activity - 0 -> 49

========================================================================
VERIFICATION PASSED - all 19 checks passed
========================================================================
```

The baseline of `clients=0, journal_entries=0` confirms the volume really was
empty, so the 49 journal entries were generated by Fineract during this run.
Of those, only a small number are the generator's manual entries — the rest are
automatic double-entry postings from the savings and loan transactions.

---

## Design decisions

**Images are pinned** to `apache/fineract:1.12.1` and `postgres:16.1`.
Fineract's `latest` and `develop` tags are rebuilt daily from trunk, which would
make runs non-reproducible.

**TLS is disabled; Fineract serves plain HTTP on 8080.** Upstream defaults to a
self-signed certificate on 8443, which forces every client and every telemetry
scraper to disable certificate verification. This is a local-only prototype, so
plain HTTP is the simpler and more honest option. *This must not carry over to
the AWS phase.*

**No restart policy is set on either container.** Docker restarting a container
on its own would silently undo exactly the failures Phase 5 needs to inject and
observe.

**Spring profile is `diagnostics` only.** Upstream's compose also sets `test`,
which only adds internal test-only API resources; leaving it off keeps the
exercised surface to the real banking APIs without changing banking behaviour.

**Prometheus metrics are enabled but no Prometheus/Grafana containers are
included** — collection is Phase 3, and belongs in `telemetry/`, not here.

**Interest recalculation is off** on the loan product, since it schedules extra
background jobs that would add noise to the Phase 3 telemetry baseline.

---

## Limitations and assumptions

- **Fineract is a representative workload, not a complete bank.** It covers
  clients, accounts, savings, loans, transactions and accounting. It is not a
  full retail banking platform and is not presented as one.
- **No external payment ecosystems are implemented.** UPI, ATM and card
  networks, RTGS and SWIFT are explicitly *not* built, simulated or stubbed
  here. The payment types the generator creates (cash, internal transfer,
  cheque) are Fineract's own labels for how a transaction was tendered inside
  this instance — they are not integrations with any external network. If such
  dependencies are needed later, they belong in the digital twin as dependency
  placeholders, not as features of this module.
- **All data comes from a running Fineract instance.** The generator creates
  activity through the REST API and records what Fineract returns. No banking
  data or telemetry is fabricated.
- **Client names are randomly generated** from small fixed name lists, and
  amounts are randomly drawn from plausible ranges. These are inputs to real
  API calls, not invented output records.
- **Single tenant, single office, single currency (INR).** Multi-tenancy,
  branch hierarchies and FX are out of Phase 2 scope.
- **All operations post on one business date** (today by default). The
  generator does not simulate a multi-day banking calendar or run Fineract's
  end-of-day batch jobs.
- **Credentials are the seeded Fineract defaults** (`mifos`/`password`) and the
  local values in `.env.example`. They are local-only development values for a
  throwaway prototype. The AWS phase must source credentials from IAM /
  Secrets Manager instead.
- **Loan repayment amounts are approximate.** The generator repays roughly
  `principal / 12` rather than reading each loan's exact amortisation schedule.
  Fineract applies the payment correctly regardless; the schedule is authoritative.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `docker compose up` warns variables are not set | `.env` is missing — run `cp .env.example .env` |
| Fineract stays `health: starting` past ~3 minutes | Check `docker compose logs fineract`; usually the database was not reachable at boot |
| Port 8080 or 5432 already in use | Change `FINERACT_HOST_PORT` / `POSTGRES_HOST_PORT` in `.env` (also update `FINERACT_BASE_URL`) |
| Generator reports connection refused | The stack is not up, or is still migrating — check `docker compose ps` |
| Init script did not run after editing it | The script runs only on an empty volume; `docker compose down -v` first |
| Odd Liquibase or schema errors after upgrading the image | Reset the volume: `docker compose down -v && docker compose up -d` |

---

## Where this fits

Phase 2 provides the workload. It deliberately stops at the boundary of the
other owners' modules:

- **Phase 3 (telemetry, Owner B)** consumes the Prometheus endpoint enabled
  here, plus container and PostgreSQL metrics. Collector configuration belongs
  in `telemetry/`.
- **Phase 4 (digital twin, Owner A)** models the `fineract → postgres`
  dependency this stack makes concrete.
- **Phase 5 (failure injection)** acts on these containers — which is why
  neither is allowed to restart itself.
