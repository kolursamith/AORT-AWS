# Security model

The protected assets are ledger integrity, authoritative status evidence, switch availability, experiment validity, and secrets. Mock banks, telemetry sources, event delivery, dependencies, CI input, and operators are trust boundaries.

| Threat | Starter defense | Next hardening step |
|---|---|---|
| Status-check flooding | per-bank identity hook, locked fixed-window quota, route credits, bounded queue, structured 429 | distributed limiter, fair scheduling, metrics and load tests |
| Telemetry poisoning | strict schema, allowlisted source, timestamp/sequence checks, outlier-driven low confidence | signatures/mTLS, independent measurement, calibrated detector |
| Replay/reordering | idempotency operation key, version compare, unique constraints, atomic ledger/outbox, audit shape | persistent nonce store and concurrency tests on PostgreSQL |
| Secret leakage | `.gitignore`, gitleaks CI, placeholders only | repository secret scanning and rotation runbook |
| PR policy tampering | ownership script checked out from base branch | protected `.github/` changes plus verified rulesets |

Attack generators refuse non-allowlisted targets and exist solely for local or isolated mock environments. DNS allowlisting is not sufficient for a production security boundary; a dedicated test VPC/namespace and egress deny rules are required before distributed tests.

Never commit AWS credentials, customer identifiers, real account data, or bank endpoints. Rotate any credential that reaches Git history even if later deleted.
