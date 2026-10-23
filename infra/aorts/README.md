# AORTS integrated system: local Docker runbook (Stage 11)

Synthetic mock banking environment only. No real bank, NPCI/UPI endpoint or customer data is
used, and nothing here is production-ready. Run every command from the repository root.

## What runs

| service | role | host port (127.0.0.1) |
|---|---|---|
| `db` | PostgreSQL 16: authoritative ledger, outbox, evidence, telemetry windows, audit chain | 55442 |
| `migrate` | one-shot: `alembic upgrade head` + synthetic seed accounts | - |
| `switch` | mock payment switch: admission (quotas, rate limits, credits), signed responses | 18110 |
| `beneficiary-b`, `beneficiary-c` | mock beneficiary banks B and C | 18121, 18122 |
| `remitter` | remitter bank service (A1/A2/A3 identities), evidence recording, ambiguity | 18100 |
| `control` | Track B control API: metrics, admin/fault controls, `/api/v1/aorts/v11/summary` | 18130 |
| `controller` | Track A v1.1 controller: route + transaction twins, estimator, look-ahead, optimizer | 18140 |
| `aorts-loop` | telemetry producer + validator, outbox/evidence delivery, v1.1 dispatcher + execution gate | - |
| `dashboard` | Streamlit security dashboard ("AORTS v1.1 security" tab) | 18501 |

One image (`infra/aorts/Dockerfile`, the exact `pyproject.toml` pins) serves every entry point.
The Compose project is `aorts-s11`, with its own network and volume (`aorts-s11_pgdata`). It
never touches Track B's `infra/trackb` stack or any other database. Track B's legacy `/v1`
dispatcher is not started, so `aorts-loop` is the only recovery dispatcher.

## Commands

```bash
# 0. fresh dependencies for local development and tests (Python 3.11+)
python -m venv .venv && . .venv/bin/activate && pip install -e '.[dev,research,dashboard]'

# 1. secrets for the stack (random, written to infra/aorts/.env, never printed or committed)
python infra/aorts/scripts/generate_env.py

# 2. build, migrate, seed, start, and wait for every health check
docker compose -p aorts-s11 -f infra/aorts/docker-compose.yml up -d --build --wait

# 3. readiness
docker compose -p aorts-s11 -f infra/aorts/docker-compose.yml ps
curl -fsS http://127.0.0.1:18140/health && curl -fsS http://127.0.0.1:18100/api/v1/health

# 4. synthetic workload: honest traffic first (the controller needs demonstrated capacity)
docker compose -p aorts-s11 -f infra/aorts/docker-compose.yml exec aorts-loop \
  python -m backend.aorts.integration.demo traffic --seconds 40 --rps 6

# 5. genuine ambiguity: the beneficiary commits, the switch response is lost
docker compose -p aorts-s11 -f infra/aorts/docker-compose.yml exec aorts-loop \
  python -m backend.aorts.integration.demo ambiguous --n 5 --dest B

# 6. follow one payment through decision, gate, check, evidence and ledger
docker compose -p aorts-s11 -f infra/aorts/docker-compose.yml exec aorts-loop \
  python -m backend.aorts.integration.demo wait-resolved <TXN_ID>
docker compose -p aorts-s11 -f infra/aorts/docker-compose.yml exec aorts-loop \
  python -m backend.aorts.integration.demo trace <TXN_ID>
docker compose -p aorts-s11 -f infra/aorts/docker-compose.yml logs --tail 20 aorts-loop

# 7. dashboard: open http://127.0.0.1:18501 and choose the "AORTS v1.1 security" tab

# 8. evidence export (JSON + SHA-256 manifest into infra/aorts/exports/)
docker compose -p aorts-s11 -f infra/aorts/docker-compose.yml exec aorts-loop \
  python -m backend.aorts.integration.demo export --out /app/results/stage11-export

# 9. safe shutdown (keeps the volume) / full disposal of this stack's volume only
docker compose -p aorts-s11 -f infra/aorts/docker-compose.yml down
docker compose -p aorts-s11 -f infra/aorts/docker-compose.yml down -v
```

End-to-end and security tests run against a disposable PostgreSQL (see `docs/final/RUNBOOK.md`):

```bash
AORTS_TEST_DATABASE_URL=postgresql+psycopg://aorts:<pw>@127.0.0.1:<port>/aorts_test \
AORTS_REQUIRE_DB=1 pytest tests
```

## Notes and limitations

- `AORTS_ESTIMATOR_PROFILE=fast` compresses the Stage 06 estimator's time constants from
  minutes to seconds so that a demonstration reaches a decision within about a minute. It is a
  declared demonstration setting, not a validated operating point; `default` restores Stage 06.
- Check spacing is a fixed 1 s (`AORTS_CHECK_BACKOFF_BASE/MAX_SECONDS=1.0`): Track B's
  exponential backoff is not expressible as one v1.1 timing policy.
- The controller is trusted to compute, not to act. Every decision is re-gated by Track B
  against authoritative rows, and no controller output can authorise a financial operation.
- After a controller restart, the operator procedure is to re-send the outbox (intake is
  idempotent). v1.1 has no controller instance id, so this is not detected automatically.
