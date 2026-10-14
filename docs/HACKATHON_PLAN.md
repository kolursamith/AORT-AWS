# 48-hour working MVP

## Hours 0–8: foundation

- Both developers install and run tests independently.
- Freeze v1 contracts and example fixtures.
- Create/protect branches after GitHub authentication.
- Demonstrate simulator and all health endpoints.

## Hours 8–24: vertical slice

- Intelligence owner: emit telemetry, maintain route/transaction twins, compare fixed/backoff/safe-budget behavior, export metrics.
- Backend owner: persist synthetic transactions, add migrations, connect beneficiary evidence, enforce idempotency/version/ledger transaction.
- Integrate one timeout → ambiguous → budgeted inquiry → resolved path through contracts.

## Hours 24–40: attacks and measurements

- Run clean baseline, flooding, poisoning, and replay scenarios with fixed seeds.
- Record goodput, inquiry load, queue depth, median/p95 resolution, unresolved-before-T+1 proxy, and invariant violations.
- Add dashboard plots and CloudWatch-compatible structured logs.

## Hours 40–48: hardening and demo

- Cross-review, full tests, Compose rebuild, teardown/restart test.
- Prepare a scripted five-minute demo and failure fallback.
- State limitations; do not claim real-bank readiness, RPO improvement, or proven novelty.
