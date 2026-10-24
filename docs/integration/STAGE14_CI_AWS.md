# Stage 14: CI, GitHub integration and AWS readiness

AWS deployment is **not authorised** and nothing was deployed. No AWS credentials were used.

## CI changes (`.github/workflows/ci.yml`, `security.yml`)

| historical issue / requirement | change |
|---|---|
| CI installed only `.[dev]`, so SimPy research tests were silently **skipped** (`pytest.importorskip("simpy")`) | install `.[dev,research,dashboard]`; after pytest, `.github/scripts/assert_no_skips.py junit.xml` fails the job if any test was skipped or none ran |
| CI ran only on `developer`/`main` | also runs on pushes to `feature/aorts-integration` (pull requests unchanged) |
| no migration check | upgrade, downgrade, re-upgrade of all Alembic migrations on a scratch PostgreSQL database |
| no compile check | `python -m compileall -q backend contracts intelligence` |
| no container check | new `containers` job: Compose config validation, build of `infra/aorts/Dockerfile` and `infra/trackb/Dockerfile` (not pushed), and an import smoke test of every entry point in the integrated image |
| security workflow ran only on `developer`/`main` | also on `feature/aorts-integration` (pip-audit + gitleaks) |

Unchanged mandatory gates: `ruff format --check`, `ruff check`, contract schema regeneration
diff, and the full pytest suite with PostgreSQL 16 and `AORTS_REQUIRE_DB=1`.

## Local verification

CI_RESULTS

## Dependency pins and security checks

* Exact pins in `pyproject.toml` (runtime, `dev`, `research`, `dashboard`). The integrated
  image installs exactly these pins, read from `pyproject.toml` at build time.
* `pip-audit` (2.9.0) over an environment with every extra installed:
  **"No known vulnerabilities found"** (2026-10-24). This came after two pin fixes:
  * `streamlit` 1.50.0 → 1.66.1 (PYSEC-2026-212, PYSEC-2026-2285);
  * `pillow` pinned to 13.0.0. Streamlit pulled in 11.3.0, which has 50 advisories; 13.0.0
    is the first fixed release that streamlit 1.66.1 allows.

  `setuptools` in the local virtualenv was upgraded; CI upgrades it before installing.
* Track B's own container pins (`infra/trackb/requirements*.txt`) differ from `pyproject.toml`
  (for example SQLAlchemy 2.1.4 vs 2.0.44). The integrated image and CI use the `pyproject`
  pins, which the full suite was run against. Track B's image still builds (below). Aligning
  the two sets is a team decision for Track B.
* Secret scanning: gitleaks 8.30.1 in git mode over every commit on the branch: no leaks. The
  only allowlisted findings are reviewed false positives (`file_key` run names in evidence
  manifests and one synthetic idempotency key). Generated `.env` files are git-ignored and
  were never committed.

## AWS CDK synthesis (no deployment)

`infra/trackb/cdk` (`aws-cdk-lib==2.180.0`, Python) was synthesised in an isolated virtual
environment with `python app.py`, with no AWS account or credentials:

* core stack: **125 CloudFormation resources** (17 IAM roles, 14 alarms, 8 ECS task
  definitions, 6 ECS services, a VPC with 6 subnets, RDS, SQS, S3, DynamoDB, ALB, Cloud Map);
* with add-ons (`addons=1`): **138 resources**.

Both counts match Track B's earlier record. The jsii runtime warned that Node v26.4.0 is
untested; synthesis succeeded. The template's SHA-256 is
`a528edf19c1afcf559909bbd35ecba72a9d5bc6fd103a22a0572b86211f392da`; it is not committed and can
be regenerated. The CDK app describes Track B's services only. The deployment plan for the
integrated system, the permissions it needs and the cost exposure are in
`docs/final/AWS_READINESS.md`.

## Architecture compatibility

The integrated image was built and run on linux/arm64 (Apple Silicon, Docker Desktop
29.6.1). CI builds it on linux/amd64 (GitHub `ubuntu-24.04`). All dependencies are pure Python
or have wheels for both platforms (psycopg binary, numpy, pandas, pillow).

## Merge readiness (inspected, nothing merged)

`feature/aorts-integration` contains every commit of `main`, `developer`,
`feature/intelligence` and `feature/backend` (0 behind each). It is ahead of `developer`, and
`git merge-tree` shows no conflicts. No pull request is open. Merging into `developer`/`main`
needs the team's review and the ownership workflow; it was **not** done.
