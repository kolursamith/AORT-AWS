# AWS deployment

**Owner: Owner B · Phase 10**

> ## ⚠️ NOTHING HERE HAS BEEN DEPLOYED
>
> No AWS account, credentials or resources were used to produce this. The
> template is **validated offline only** (cfn-lint plus structural and security
> tests, 20 checks). Deploying costs real money and is your decision — see
> [Cost](#cost) and [Before you deploy](#before-you-deploy).
>
> Treat every number below as an estimate from public pricing, **not** a
> measured bill.

---

## What it provisions

Only the services classified **CORE** in the repository audit. The research
chain is what matters, not the AWS service count.

| Service | Why it earns its place |
|---|---|
| **EC2** (1 × t3.medium) | Runs the existing banking and telemetry stacks unchanged |
| **S3** | Ledger backups **off the host they protect** — the local sidecar alone is not a DR position |
| **IAM** | Least-privilege role: this bucket's `backups/` prefix, this log group, metrics in one namespace |
| **CloudWatch Logs** | AWS-side telemetry, 7-day retention |

**Deliberately absent:** RDS, Lambda, Step Functions, EventBridge, DynamoDB,
SageMaker, API Gateway, Cognito, WAF/Shield, QuickSight, Fault Injection
Simulator and Elastic Disaster Recovery. A test asserts the template contains
none of them. Each was classified deferred or unjustified because it does not
materially support the chain telemetry → twin → scenario → prediction →
recovery → measurement.

> Objective 5 in the Phase-I documents names Elastic DR, Step Functions and
> multi-region failover. Those are **not** implemented. They remain open
> decisions — model them as strategies the optimizer *scores*, or re-scope the
> objective.

## Security posture

- **SSH is not open by default.** `AllowedSshCidr` defaults to a range that
  cannot reach the instance; you must supply your own. A world-reachable SSH
  port on a host holding banking backups is not an acceptable default.
- **Dashboards are not published.** Fineract, Prometheus and Grafana are
  reachable only through the SSH tunnel printed in the stack outputs.
- **Bucket:** all public access blocked, SSE-S3 encryption, versioning on, and
  a bucket policy denying non-TLS transport.
- **Backups survive the stack.** The bucket is `Retain` on delete: removing the
  experiment must not remove the evidence it produced.
- **EBS encrypted**, AMI resolved from the SSM public parameter (no hard-coded
  AMI ids), no secrets in the template.

The IAM role can write only to `backups/*` in its own bucket, write only to its
own log group, and publish metrics only under `AORT/Banking`. A test fails the
build if any policy grants `Action: "*"`, or `Resource: "*"` for anything other
than `cloudwatch:PutMetricData` with a namespace condition.

## Before you deploy

1. **Decide whether to spend money at all** — the local stack already
   demonstrates the full chain.
2. Pick a region and an existing VPC, public subnet and EC2 key pair.
3. Set `AllowedSshCidr` to your own address.
4. Set a billing alarm first.

```bash
aws cloudformation deploy \
  --template-file aws/cloudformation/aort-prototype.yml \
  --stack-name aort-prototype \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
      KeyName=<your-key> \
      AllowedSshCidr=<your.ip.address>/32 \
      VpcId=<vpc-...> \
      SubnetId=<subnet-...>

aws cloudformation describe-stacks --stack-name aort-prototype \
  --query 'Stacks[0].Outputs'
```

Boot takes roughly 5–10 minutes: the instance installs Docker, clones this
repository, starts both stacks and waits for Fineract's Liquibase migrations.

Tear down with `aws cloudformation delete-stack --stack-name aort-prototype`.
**The bucket is retained deliberately** — empty and delete it by hand when you
are finished with the evidence.

## Cost

Rough `ap-south-1` on-demand estimate, **not** a measured bill:

| Item | Estimate |
|---|---|
| t3.medium, 24×7 | ~US$30/month |
| 30 GB gp3 | ~US$2.50/month |
| S3 (a few GB, 30-day expiry) | well under US$1/month |
| CloudWatch Logs (7-day retention) | well under US$1/month |
| **Total, running continuously** | **~US$35/month** |

Stopping the instance between sessions removes almost all of it.

## Can the Free Tier cover this?

**Yes, for a project of this size — but on credits, not on free hours, and the
instance must be one of the larger Free Tier eligible types.**

### What the stack actually needs

Measured with `docker stats` against the full running stack, not estimated:

| Container | Resident memory |
|---|---|
| fineract | 1.17 GiB |
| grafana | 601 MiB |
| postgres | 261 MiB |
| cadvisor | 149 MiB |
| prometheus | 104 MiB |
| otel-collector | 44 MiB |
| postgres-exporter | 17 MiB |
| node-exporter | 9 MiB |
| db-backup | 1 MiB |
| **Total** | **3.19 GiB** |

Add the host OS and Docker itself and the floor is roughly 4 GiB. Fineract
alone exceeds a 1 GiB instance, so **t3.micro cannot run this**, and t3.small
(2 GiB) cannot either.

### What the Free Tier offers

AWS replaced the Free Tier on **2025-07-15**, and the two schemes are not alike:

| | Account created before 2025-07-15 | Account created on or after 2025-07-15 |
|---|---|---|
| Eligible EC2 types | `t2.micro`, `t3.micro` | `t3.micro`, `t3.small`, `t4g.micro`, `t4g.small`, `c7i-flex.large`, `m7i-flex.large` |
| Model | 750 instance-hours/month | USD $100 sign-up credit, up to $100 more earned |
| Duration | 12 months | 6 months, or until credits run out |

The newer scheme is the useful one here: **`m7i-flex.large` (2 vCPU, 8 GiB) and
`c7i-flex.large` (2 vCPU, 4 GiB) are Free Tier eligible** and both clear the
3.19 GiB measurement. The older 750-hour scheme is useless to us — every type
it covers is too small.

### Does the credit stretch far enough?

At `us-east-1` list price for `m7i-flex.large` (~US$0.0958/hour — a published
list rate, not a bill we have paid, and Mumbai is somewhat higher):

| Budget | Continuous hours | In practice |
|---|---|---|
| $100 sign-up credit | ~1,040 h (~43 days) | ~260 four-hour experiment sessions |
| $200 with earned credits | ~2,080 h | capped by the 6-month expiry first |

A disaster-recovery experiment runs for minutes, not weeks. Even the sign-up
credit alone is far more than this project can consume, **provided the instance
is stopped between sessions.** Left running 24×7 it burns the whole $100 in
about six weeks.

### The catch worth knowing before you sign up

On the **free plan**, when credits run out *the account closes* and AWS keeps
your data for 90 days. This stack's whole point is producing evidence, and the
S3 bucket is `Retain` precisely so evidence survives stack deletion — account
closure defeats that. Two ways to avoid it:

- Pull the backups and measurement outputs off S3 before the 6 months expire, or
- Choose the **paid plan** and set an AWS Budgets alarm at a few dollars, so
  credits still apply but the account does not close underneath the evidence.

### Recommendation

Local-first still stands: the stack demonstrates the full chain on a laptop and
every result in this repository was produced that way. Use AWS for a deliberate
demonstration window — `m7i-flex.large`, started for the session, stopped after
— rather than as the permanent home of the experiment.

Free Tier terms change; re-read [aws.amazon.com/free](https://aws.amazon.com/free/)
before relying on any of the above.

## Files

| Path | Purpose |
|---|---|
| `cloudformation/aort-prototype.yml` | The stack |
| `scripts/user-data.sh` | Bootstrap: Docker, clone, start both stacks, install the shipper |
| `scripts/backup-to-s3.sh` | Ships ledger dumps to S3 every 5 minutes via cron |
| `tests/test_template.py` | Offline validation (cfn-lint, security, cost, operability) |

## Tests

```bash
pip install cfn-lint pytest
cd aws && pytest
```

20 checks, none requiring AWS: cfn-lint reports no errors or warnings, only
CORE services appear, deferred services are absent, no security group is open
to the world, the bucket blocks public access and is encrypted and versioned,
IAM has no wildcards, no secrets are embedded, log retention and lifecycle
expiry are bounded, and the outputs give an operator what they need.

cfn-lint caught a real defect during development: an apostrophe in a security
group description violates the AWS-allowed character pattern (E3031), which
would have failed at deploy time.

## Limitations

- **Never deployed, so never verified against AWS.** Offline validation cannot
  prove the instance boots, the user data succeeds, or IAM permissions suffice
  in practice. The first real deployment should be treated as a test.
- Single instance, single AZ: **no high availability and no failover.** It
  demonstrates a cloud deployment of the workload, not a resilient architecture.
- PostgreSQL runs in a container on the instance, not RDS, so there are no
  managed snapshots or point-in-time recovery.
- CloudWatch receives logs; the Prometheus metrics are **not** forwarded to
  CloudWatch yet, so AWS-side metrics remain sparse.
- No CI deployment, no blue/green, no secrets manager: `.env.example` values
  are copied on boot and must be changed for anything beyond a prototype.
