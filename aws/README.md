# aws/

**Owner: Owner B · Phase 10 — AWS deployment · Status: not started**

Cloud deployment and AWS-side integration: EC2, RDS PostgreSQL, S3,
CloudWatch and IAM.

Introduced **after** the local system, telemetry, twin and experiments work, per
the blueprint's local-first cost principle. Never add a service just because it
is available — justify it in the PR.

Two local-only Phase 2 shortcuts must **not** carry over: disabled TLS, and
local development credentials. The cloud phase sources credentials from IAM /
Secrets Manager.
