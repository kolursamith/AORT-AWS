# AWS readiness (Stage 14): plan only, nothing deployed

**Status: not deployed.** No AWS account, credentials or billable resource was used or
created. Deployment needs separate, explicit authorisation from the project owners.

## What exists

* `infra/trackb/cdk/` is a Python AWS CDK app (`aws-cdk-lib==2.180.0`) for the Track B
  services. Its synthesis result is recorded in `docs/integration/STAGE14_CI_AWS.md`.
* `infra/aorts/Dockerfile` builds a single image with every integrated entry point (Track A
  controller, `aorts-loop`, Track B services, dashboard). The CI `containers` job builds it on
  `linux/amd64`. It was also built and run locally on `linux/arm64` (Docker Desktop, Apple
  Silicon).

## What a deployment of the integrated system would add to the Track B stack

| addition | AWS service | notes |
|---|---|---|
| Track A controller | ECS Fargate service (0.25 vCPU / 0.5 GB), Cloud Map `controller.aorts.local:8140` | config from `worker controller-config`; bearer token from Secrets Manager |
| `aorts-loop` (telemetry, delivery, v1.1 dispatcher) | ECS Fargate service, no inbound port | replaces Track B's legacy dispatcher service (only one dispatcher per database) |
| backend ↔ controller token | Secrets Manager secret | never in task definitions or images |
| migration `0002` | the existing one-shot `migrate` task | additive; test downgrade first on a snapshot |

## Permissions a deployer would need (least privilege, for `cdk deploy`)

* CloudFormation, plus the CDK bootstrap roles (`cdk bootstrap` once per account and region).
* Creation and management of: EC2 VPC, subnets, NAT, security groups, ECS, ECR (image
  assets), RDS, Secrets Manager, SQS, S3, DynamoDB, CloudWatch Logs, Elastic Load Balancing,
  Cloud Map, and IAM role creation for task roles.
* With add-ons: EventBridge, Lambda and Step Functions.

## Cost exposure (estimate, not a quote)

The fixed monthly cost comes from these always-on parts:

* one NAT gateway;
* the internet-facing ALB;
* an RDS `db.t4g.micro` with 20 GB of storage;
* 8 Fargate tasks at 0.25 vCPU / 0.5 GB (6 Track B services, the controller and `aorts-loop`);
* CloudWatch Logs ingestion.

At public list prices in `ap-south-1`, an always-on stack like this costs on the order of
**USD 100-150 per month** before data transfer. The NAT gateway and the ALB are the largest
items. Use the AWS Pricing Calculator for a real figure before any deployment. To keep
exposure low:

* deploy only for a demonstration window, then `cdk destroy`;
* keep `AllowedCidr` at a single address;
* set an AWS Budgets alarm;
* keep log retention at one week (as configured).

## Not ready / open items

* No deployment has been verified in AWS. Health checks, Cloud Map naming and Secrets
  Manager wiring are untested there.
* The CDK app describes Track B only. Adding the controller and `aorts-loop` services is a
  reviewed change still to be made.
* The research estimator profile (`fast`) must not be used outside a demonstration.
