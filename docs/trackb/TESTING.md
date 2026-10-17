# AORTS Track B — Testing

## Running the tests

```powershell
.\aorts.ps1 db                 # start PostgreSQL only (docker compose up -d db)
.\aorts.ps1 test               # whole suite
.\aorts.ps1 test-security      # security-focused subset
```

(With Make: `make db`, `make test`, `make test-security`.)

The tests need PostgreSQL, because row locks, unique constraints, `SKIP LOCKED` and
advisory locks are part of what is tested; SQLite would hide concurrency bugs. Each
session drops and re-creates a separate database, `aorts_test`, **through the Alembic
migrations**, so the migrations are tested every time. The URL is taken from `.env`
(`AORTS_DATABASE_URL` with the database name replaced) or from
`AORTS_TEST_DATABASE_URL`. The tests make no external network calls. The whole stack
runs in-process through FastAPI `TestClient`s, but every hop is still a real signed HTTP
request.

> Windows note: use `127.0.0.1`, not `localhost`, in database URLs. Postgres is bound to
> 127.0.0.1 only, and `localhost` tries IPv6 `::1` first, which can stall each new
> connection for minutes.

**Last verified run (this machine, 2026-10-17):** `175 passed` (full suite, with PostgreSQL). In the team repository layout (`kolursamith/AORT-AWS`, `feature/backend`, with that repository's pinned dependencies): `195 passed`, which includes the other track's tests.

## Coverage map

| Group | File(s) | What is checked |
|---|---|---|
| A. API | [test_payment_flow.py](tests/integration/test_payment_flow.py) | valid / invalid / float amounts, insufficient funds, unknown route, duplicate idempotency key (sequential and 8 concurrent), key reuse with a different body, lost response → AMBIGUOUS, switch unavailable, unsigned request, simulation header needs admin, paginated history |
| B. Ledger | [test_ledger_invariants.py](tests/test_ledger_invariants.py) | transition map, state projection, timeout ≠ evidence, unknown outcome blocks reversal, unauthenticated evidence, PENDING preserves uncertainty, reversal exactly once and balanced, causal link to the debit, operation id cannot be reused across transactions, stale version, tampering detected, DB-level non-negative balance, quarantine |
| C. Flooding | [test_rate_limits.py](tests/test_rate_limits.py) | bank quota and isolation, spoofed bank id, credit exhaustion without consuming buckets, route isolation, global ceiling, Tier 2 allocation, a real flood rejected **before** the switch core and downstream, defenses-off arm, spoofing during a flood |
| D. Telemetry | [test_telemetry_integrity.py](tests/test_telemetry_integrity.py) | source auth and spoofing, schema, stale/future/duplicate/out-of-order, impossible ranges, p95 < p50, missing values, optimistic false throughput, budget cannot rise, conservative fallback, headroom cap, EWMA, uncorroborated claims, all poisoning cases (protected bounds harm), no false positives during a real degradation, subtle-poisoning limitation |
| E. Replay | [test_replay_protection.py](tests/test_replay_protection.py), [tests/security/](tests/security/) | sequential duplicate, stale version and timestamp, repeated reversal, concurrent duplicate events (10 threads), concurrent reversals (8 threads, mixed op ids), duplicate after close, wrong transaction, wrong signer, invalid/missing idempotency key, repeated nonce, expired timestamp, tampered body, unsigned request. Runs with auto-reverse **on and off**. HMAC unit tests: rotation, path/method binding, nonce burning, response signatures |
| F. Integration | [test_recovery.py](tests/integration/test_recovery.py), [test_control_plane.py](tests/integration/test_control_plane.py) | E8 lost response resolved to SUCCESS by the dispatcher; never-received payment fenced and reversed once; beneficiary response lost after credit; Bank B down while C healthy; deadline → HANDOFF_UDIR; rate-limited check does not use up the allowance; controller down → fallback budget; duplicate SQS-style job; dashboard summary/timeseries; admin/lab protection and 404 when disabled; fault API; all three attacks through the lab API; telemetry endpoint role; outbox relay + idempotent consumer; controller dry run |
| Concurrency regressions | [test_payment_flow.py](tests/integration/test_payment_flow.py) | 24 concurrent payments from **one sender** (FK-lock deadlock regression); 36 mixed concurrent debits, settlements and reversals on shared accounts, then a dispatcher drain → all CLOSED/REVERSED, ledger ok |
| Bulkhead | [test_bulkhead.py](tests/integration/test_bulkhead.py) | an undefended flood on a shared pool makes the switch refuse (and safely reverse) payments; with the bulkhead, all payments close |
| Reconciliation | [test_reconciliation.py](tests/integration/test_reconciliation.py), control plane | escalated never-credited → RESOLVED, reversed once; escalated but credited → RESOLVED, SUCCESS (works even with Tier 2 credits exhausted); state and role checks; separate rate limit; admin-only quarantine release; dashboard reconcile endpoint |
| Baselines and metrics | [tests/unit/](tests/unit/) | baseline policy behaviour, `baseline:<name>` controller mode, per-run idempotency keys, recovery-time and CI definitions, flood recovery metric |
| Contracts | [tests/contract/](tests/contract/) | every fixture validates; float/unknown fields rejected; state machine complete |
| G. Deployment | manual + compose healthchecks | see below |

## Deployment checks (G), verified locally with Docker Compose

```powershell
docker compose up -d --build
docker compose ps                                   # every service "healthy", migrate exited 0
docker compose logs migrate                         # {'identities': 6, 'accounts': 58}
foreach ($p in 8100,8110,8121,8122,8130) { curl.exe -s http://127.0.0.1:$p/api/v1/health }
curl.exe -s -o NUL -w "%{http_code}" http://127.0.0.1:8501   # dashboard: 200
.\.venv\Scripts\python -m backend.aorts.experiments.run_end_to_end --mode http --phase 20
```

The AWS deployment checks are listed in [AWS_DEPLOYMENT.md](AWS_DEPLOYMENT.md#4-verify).
They have **not** been run: no AWS account was available.

## Load tests (Locust)

```powershell
.\.venv\Scripts\locust -f locustfile.py --host http://127.0.0.1:8100                  # UI on :8089
$env:AORTS_LOCUST_FLOOD="1"; .\.venv\Scripts\locust -f locustfile.py --headless -u 12 -r 4 -t 60s --host http://127.0.0.1:8100
```

The Locust file refuses any host other than localhost or 127.0.0.1.
