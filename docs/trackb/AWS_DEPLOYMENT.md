# AORTS Track B — AWS deployment (AWS CDK, Python)

> **Status: not deployed.** The stack in [cloud/cdk/](cloud/cdk/) has been
> **synthesized** successfully (`cdk synth`) with `aws-cdk-lib 2.180.0`:
> - core stack: 125 CloudFormation resources;
> - with `-c addons=1`: 138 resources.
>
> It has **not** been deployed or verified in an AWS account, because no AWS CLI,
> credentials or account were available in the development environment. Everything
> below is the procedure to follow once they are. Local development never needs AWS.

## 1. What gets created

| Layer | Resources | Notes |
|---|---|---|
| Network | VPC (2 AZ), public / private-with-egress / isolated subnets, **1 NAT gateway** | the NAT gateway is the main fixed cost |
| Compute | ECS cluster, 6 Fargate services (switch, beneficiary-b, beneficiary-c, remitter, control, dispatcher), each 0.25 vCPU / 0.5 GB, plus `migrate` and `experiments` task definitions | Cloud Map namespace `aorts.local` |
| Entry point | Internet-facing ALB, HTTP :80, **only `/api/v1/payments*` and `/api/v1/health`** → remitter. SG admits only `AllowedCidr` (default `127.0.0.1/32` = nobody) | no admin, lab or switch endpoints are routed |
| Data | RDS PostgreSQL 16 `db.t4g.micro`, 20 GB, encrypted, single-AZ, isolated subnets, not public; DynamoDB `TwinState` (on-demand, encrypted); SQS `DispatchQueue` + DLQ (maxReceiveCount 5, SSE, SSL-only); S3 results bucket (SSE, public access blocked, SSL-only, 90-day expiry) | |
| Secrets | RDS credentials (generated) and `AppSecrets` (`admin_token` generated; `service_keys` **you must set**) | injected into containers as ECS secrets |
| Monitoring | 1-week log groups `/aorts/<service>`; CloudWatch EMF metrics (namespace `AORTS`); 14 alarms → SNS topic; optional e-mail subscription; AWS Budget (80 % forecast) if `AlarmEmail` is set | |
| Add-ons (`-c addons=1`) | EventBridge bus `aorts-bus` → Lambda → S3 `events/`; Step Functions `ExperimentCampaign` (replay → poisoning → flooding as ECS RunTask) | deploy only after the core works |

The lab and fault controls are hard-wired **off** in every AWS service. The Step
Functions experiments run in in-process mode inside a private task, on the separate
`aorts_experiments` database.

## 2. Prerequisites

```powershell
# Tools
winget install Amazon.AWSCLI           # or the MSI from aws.amazon.com/cli
npm install -g aws-cdk@2               # CDK CLI (Node 18+)
docker --version                       # Docker Desktop must be running (image asset build)

# Credentials: use an IAM Identity Center / SSO profile. Never paste long-lived keys into files.
aws configure sso
aws sts get-caller-identity --profile <profile>

# CDK app dependencies
cd cloud\cdk
python -m venv .venv; .\.venv\Scripts\pip install -r requirements.txt
```

Check service quotas in the target region (default `ap-south-1`): 1 Elastic IP, 1 NAT
gateway, Fargate vCPU ≥ 2.

## 3. Deploy the core stack

```powershell
$env:AWS_PROFILE = "<profile>"
$env:CDK_DEFAULT_REGION = "ap-south-1"
cd cloud\cdk
.\.venv\Scripts\cdk bootstrap                      # once per account/region
.\.venv\Scripts\cdk synth  > $null                # sanity check
.\.venv\Scripts\cdk deploy AortsTrackB `
    --parameters AllowedCidr=<your.public.ip>/32 `
    --parameters AlarmEmail=<you@example.com> `
    --parameters MonthlyBudgetUsd=30
```

Expected outputs: `AlbDns`, `ClusterName`, `MigrateTaskDef`, `AppSubnets`,
`AppSecurityGroup`, `AppSecretArn`, `ResultsBucket`, `DispatchQueueUrl`, `TwinTable`.

### 3.1 Set the service keys (required)

```powershell
python scripts\generate_env.py --force      # local .env with fresh keys (never committed)
$keys = (Select-String -Path .env -Pattern '^AORTS_SERVICE_KEYS=').Line.Split('=',2)[1]
$admin = aws secretsmanager get-secret-value --secret-id <AppSecretArn> --query SecretString --output text |
         ConvertFrom-Json | Select-Object -ExpandProperty admin_token
aws secretsmanager put-secret-value --secret-id <AppSecretArn> `
    --secret-string (@{service_keys=$keys; admin_token=$admin} | ConvertTo-Json -Compress)
aws ecs update-service --cluster <ClusterName> --service <each service> --force-new-deployment
```

### 3.2 Run the database migration (one-off task)

```powershell
aws ecs run-task --cluster <ClusterName> --launch-type FARGATE --task-definition <MigrateTaskDef> `
  --network-configuration "awsvpcConfiguration={subnets=[<AppSubnets>],securityGroups=[<AppSecurityGroup>],assignPublicIp=DISABLED}"
aws logs tail /aorts/migrate --follow        # expect: {'identities': 6, 'accounts': 58}
```

## 4. Verify

```powershell
curl http://<AlbDns>/api/v1/health            # {"status":"ok","service":"remitter"}
# Signed payment from your machine (uses the same keys you stored in Secrets Manager):
$env:AORTS_REMITTER_URL = "http://<AlbDns>"
.\.venv\Scripts\python -c "from backend.aorts.experiments.common import http_client; c,s=http_client(); import uuid; print(c.post('remitter','/api/v1/payments',{'sender_account_id':'A1-CUST-0001','beneficiary_account_id':'B-CUST-0001','beneficiary_bank_id':'B','amount_paise':1000,'idempotency_key':'aws-'+uuid.uuid4().hex},as_identity='CHANNEL').json())"
aws logs tail /aorts/switch --since 10m
aws cloudwatch list-metrics --namespace AORTS
aws sqs get-queue-attributes --queue-url <DispatchQueueUrl> --attribute-names All
```

Checklist:
- ECS services are stable.
- The ALB health check is OK.
- The payment returns `CLOSED`.
- `/aorts/*` logs are JSON.
- `AORTS` EMF metrics appear.
- The DLQ is empty.
- RDS shows `PubliclyAccessible: false`.
- The `AllowedCidr` restriction works: requests from another IP time out.

## 5. Add-ons (after the core is verified)

```powershell
.\.venv\Scripts\cdk deploy AortsTrackB -c addons=1 --parameters AllowedCidr=<ip>/32
aws stepfunctions start-execution --state-machine-arn <ExperimentCampaign ARN>
aws s3 ls s3://<ResultsBucket>/experiments/ --recursive
```

AWS Fault Injection Service is **not** included. Application-level faults
(`FaultConfig`) cover the experiments. Add FIS only once the deployment is stable.

## 6. Cost control (indicative only; check the AWS Pricing Calculator for your region)

Main drivers while the stack is running:
- the NAT gateway (hourly + data);
- 6 small Fargate tasks running continuously;
- the ALB;
- RDS `db.t4g.micro`.

DynamoDB, SQS, S3, Lambda and Step Functions cost almost nothing at experiment volumes.
**Deploy, run the experiments, export the results, and destroy the stack the same day.**
Set `AlarmEmail` so the budget alert is created. To pause compute without destroying
the stack, scale every service to 0:
`aws ecs update-service --cluster <c> --service <s> --desired-count 0`.

## 7. Teardown

```powershell
aws s3 sync s3://<ResultsBucket> .\results\aws\       # keep your results first
.\.venv\Scripts\cdk destroy AortsTrackB
```

The RDS instance, buckets, log groups and table are created with `RemovalPolicy.DESTROY`,
and the bucket auto-deletes its objects, so nothing billable is left behind. Afterwards,
check in the console that no NAT gateway, Elastic IP or snapshot remains.
