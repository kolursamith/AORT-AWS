# AORTS — Track B
### A Security-Aware Digital-Twin Recovery Controller for Feedback-Safe Recovery of Ambiguous Banking Transactions

> ⚠️ **Research prototype in a fully SYNTHETIC mock environment.** It never connects to
> real banks, NPCI, UPI or customer accounts. All banks (A1, A2, A3, B, C), accounts
> (`A1-CUST-0001` …), amounts (integer paise) and identifiers are synthetic. It uses
> simplified UPI-*like* semantics; there is **no** ISO 8583 / ISO 20022 / UPI / IMPS
> compliance and no regulatory claim.

Track B is the mock banking network, its authoritative PostgreSQL ledger, the security
layer, event processing, local orchestration, the AWS infrastructure code, the dashboard
and the experiments. Track A (twin, ML, look-ahead, optimizer) plugs in through the
controller adapter described in [API_CONTRACTS.md §6](API_CONTRACTS.md#track-a-integration-proposed-confirm-with-track-a).
Until then, a mock controller keeps Track B fully runnable on its own.

## Running Track B from this team repository

The local quick-start below was written for the stand-alone project folder. In this repository
the Track B code is the package `backend/aorts/`, and the commands run **from the repository root**:

```powershell
python -m venv .venv; .\.venv\Scripts\pip install -r infra/trackb/requirements-dev.txt
.\.venv\Scripts\python infra/trackb/scripts/generate_env.py         # .env with fresh random secrets
docker compose --env-file .env -f infra/trackb/docker-compose.yml up -d --build
$env:AORTS_REQUIRE_DB = "1"; .\.venv\Scripts\python -m pytest tests/trackb
.\.venv\Scripts\streamlit run dashboard/trackb_app.py                # or the dashboard container on :8501
```

`infra/trackb/aorts.ps1` and `infra/trackb/Makefile` wrap these commands; run them from the
repository root. Experiment results are written to the local `results/` folder and are not
committed, because `results/` belongs to the other track.

## What it demonstrates

1. **Ambiguity without false failure.** A lost or delayed response makes a payment
   `AMBIGUOUS`, never "failed". Only authenticated switch evidence settles or reverses
   it. Slow beneficiaries are *fenced*, so a late credit can never duplicate money.
2. **Status-check flooding (Attack 1).** A compromised bank A3 floods `/check-status`.
   Per-bank, per-route and global token buckets plus Tier 2 credits reject the excess
   *before* it reaches the switch core or beneficiaries; A1 and A2 keep their capacity.
3. **Twin telemetry poisoning (Attack 2).** False latency or throughput reports are
   cross-checked against the switch's independent measurements. Suspicious telemetry
   can never raise the status-check budget; a conservative fallback applies instead.
4. **Transaction replay (Attack 3).** Duplicate, stale, mis-linked, concurrent and
   forged events or reversals cannot create a second financial effect: HMAC + nonce,
   event-id uniqueness, version checks and database constraints stop them.

## Quick start (Windows PowerShell; no Make needed)

Prerequisites: Python 3.11+ (tested with 3.14 on the host and 3.12 in Docker), Docker
Desktop running.

```powershell
cd "CAD PROJECT-2"
.\aorts.ps1 setup          # .venv + dependencies + .env with fresh random secrets (git-ignored)
.\aorts.ps1 up             # build + start: postgres, migrate/seed, switch, banks B/C, remitter, dispatcher, control, dashboard
docker compose ps          # all services healthy
.\aorts.ps1 test           # 137 automated tests (needs only the postgres container)
.\aorts.ps1 dashboard      # opens http://127.0.0.1:8501
```

With Make: `make setup && make up && make test`.

| Service | URL (bound to 127.0.0.1 only) |
|---|---|
| Remitter bank (A1/A2/A3) | http://127.0.0.1:8100/docs |
| Payment switch | http://127.0.0.1:8110/docs |
| Beneficiary bank B / C | http://127.0.0.1:8121/docs · http://127.0.0.1:8122/docs |
| Control / lab API | http://127.0.0.1:8130/docs |
| Dashboard | http://127.0.0.1:8501 |
| PostgreSQL | 127.0.0.1:55432 (user `aorts`, password in `.env`) |

The dashboard runs in Docker and reads `AORTS_ADMIN_TOKEN` from `.env`, so its demo
controls are enabled locally. To run it outside Docker:
`$env:AORTS_ADMIN_TOKEN=(...); .\.venv\Scripts\streamlit run backend/aorts/dashboard/app.py`.

## Demo script (≈10 minutes)

1. **Dashboard → sidebar → Start traffic** (5 payments/s, 60 s). *Overview* shows
   payments closing; *Security* shows A1/A2 status checks.
2. **Slow down Bank B.** B payments become *ambiguous* (not failed) while C keeps
   closing. *Routes & recovery* shows the dispatcher's decisions: the recommended vs
   guard-applied budget and the fallback reason.
3. **Restore normal.** The backlog drains through real status checks, and the ledger
   integrity banner stays green.
4. **Run status-check flooding (off vs on).** *Experiments* shows admitted checks per
   bank in each arm, plus detection and containment time.
5. **Run telemetry poisoning.** *Security → telemetry verdicts* shows the rejected or
   suspicious samples. *Experiments* shows the simulated delay per arm.
6. **Run transaction replay.** All 21 cases are listed with their rejection codes, and
   ledger verification is `ok`.
7. **Audit trail → Verify hash chain.**

Command-line equivalents:
```powershell
.\.venv\Scripts\python -m backend.aorts.experiments.run_end_to_end --mode http --phase 20   # E1/E2/E3/E8 on Docker
.\aorts.ps1 experiments                                                            # E4/E5/E6/E9/E10 (+ summaries)
```
Results go to `results/raw/*.json` (full detail) and `results/processed/*.csv`, and
`summary-*.csv` holds the mean, std and 95% CI. Measured results from this machine are
summarised in [RESULTS.md](RESULTS.md).

## Recovery policy comparison

`AORTS_CONTROLLER_MODE` selects the recovery policy:
- `mock`, the default stand-in;
- `http`, Track A's AORTS controller;
- `baseline:<name>`, a reference policy: `naive-polling`, `npci-static` (a simplified simulation
  schedule, not a regulatory rule), `exp-backoff-jitter`, `fixed-budget` or `aimd`.

`python -m backend.aorts.experiments.run_policy_comparison` replays one degraded-route workload against
each policy, with and without Track B's budget guard. Add `--policies http` to include Track A.

## Digital twins, AORTS controller and strategy comparison

- **Twins and AORTS recovery intelligence:** `backend/aorts/twin/` and `backend/aorts/recovery/`. Select the
  controller with `AORTS_CONTROLLER_MODE=aorts`. The dashboard's *Twins & AORTS* tab shows twin
  state, capacity estimates and confidence, the safe budget with its reasons, the selected
  transactions, and predicted-versus-observed values.
- **Strategy comparison** on identical seeded workloads, against the Docker stack:
  ```powershell
  docker compose -f docker-compose.yml -f docker-compose.experiments.yml up -d   # experiment profile
  .\.venv\Scripts\python -m backend.aorts.experiments.run_strategy_comparison --scenario S1-degraded-B --seeds 30
  .\.venv\Scripts\python -m backend.aorts.experiments.analyze_strategy_comparison
  docker compose up -d                                                           # back to the normal profile
  ```
- **Protected security acceptance tests:** `tests/acceptance/`. Run them with `.orts.ps1 test-security`.
- **Technical evidence pack:** [docs/track-b-technical-evidence/](docs/track-b-technical-evidence/).

## Repository layout

```
aorts/
  app/         config, logging (JSON, redacted), db, metrics (EMF), signed HTTP client, seed, local in-process stack
  contracts/   versioned Pydantic schemas + JSON fixtures (schema 1.0)
  banking/     remitter/ switch/ beneficiary/ ledger/ (models, migrations, service, invariants) dispatcher/ faults/
  security/    request_auth, nonce_store, rate_limiter, tier2_credits, defense_policy, telemetry_validator,
               anomaly_detector, budget_guard, replay_protection, audit, attack_config, *_simulator
  events/      outbox, publisher (relay), consumer (idempotent), eventbridge_adapter
  adapters/    controller_client (HTTP + resilient fallback), controller_mock, twin_state (+Postgres CAS), dynamodb_adapter
  control/     ops/lab API: metrics read models, telemetry ingestion, lab orchestration, experiments, traffic
  dashboard/   Streamlit app + API client
  experiments/ run_flooding, run_poisoning, run_replay, run_end_to_end, compare_results, export_metrics
cloud/         cdk/ (AWS CDK Python stack + Lambda), monitoring/, deployment.md
tests/         unit/ integration/ security/ contract/ + top-level module tests
results/       raw/ processed/ figures/  (generated, git-ignored)
```

The brief's `security/state_integrity.py` and `invariant_enforcer.py` are covered by
`banking/ledger/invariants.py` + `service.py` (the safety engine) and
`security/budget_guard.py`. The brief's single `app/main.py` became one
`main.py` per service.

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md): components, payment workflow, fencing, recovery
  loop, limitations.
- [SECURITY.md](SECURITY.md): threat model, HMAC, rules A–J, the three modules, audit
  chain, residual risks.
- [API_CONTRACTS.md](API_CONTRACTS.md): every endpoint, schema, error code, and the
  Track A contract.
- [TESTING.md](TESTING.md): how to run the tests and what each group covers.
- [AWS_DEPLOYMENT.md](AWS_DEPLOYMENT.md): CDK deploy, verify, cost, teardown.
  **Synthesized, not deployed.**
- [RESULTS.md](RESULTS.md): measured experiment results and their caveats.

## Configuration

Settings come from environment variables `AORTS_*` or `.env`. See
[.env.example](.env.example) and [config.py](backend/aorts/app/config.py). To regenerate secrets:
`python infra/trackb/scripts/generate_env.py --force` (it never prints them).

Important switches:
- `AORTS_ENABLE_ATTACK_CONTROLS` / `AORTS_ENABLE_FAULT_CONTROLS`: off by default, on
  locally via `.env`, and forced off in AWS.
- `AORTS_CONTROLLER_MODE=mock|http` with `AORTS_CONTROLLER_BASE_URL`.
- `AORTS_DEADLINE_SECONDS`: the *simulated* T+1 deadline, in real seconds.

## Known limitations

- Shared symmetric HMAC keys (no PKI/mTLS); in local mode every service holds the whole
  keyring.
- The nonce cache, rate-limit buckets and Tier 2 credits are in-memory and
  single-replica (one switch task).
- One PostgreSQL instance holds every bank's tables (logically separated), and there is
  no cross-service 2PC. Consistency comes from idempotency, evidence rules, fencing and
  escalation.
- The telemetry-poisoning experiment uses a fluid-queue route model as ground truth (a
  simulation). Subtle lies inside the tolerance are not detected; they are only bounded
  by the independent headroom cap.
- The audit chain is tamper-evident, not tamper-proof; there is no external anchoring.
- AWS: the ALB is HTTP only, the stack was not deployed, and Fault Injection Service is
  not included.
- Track A integration uses a proposed HTTP contract that Track A still has to confirm.
