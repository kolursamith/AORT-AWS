# AWS Infrastructure (Phase-II)

Planned AWS resources and infrastructure-as-code for AORT deployment.

## Planned Implementation

| Area | AWS Services |
|------|-------------|
| Compute | EC2, ECS, Lambda |
| Networking | VPC, Route 53, CloudFront, ALB |
| Data | RDS, S3, AWS Backup |
| Integration | API Gateway, SQS, SNS, Step Functions |
| DR | Elastic Disaster Recovery, multi-region failover |
| Security | IAM, Cognito, KMS, CloudTrail, Config |
| Observability | CloudWatch dashboards and alarms |
| IaC | AWS CloudFormation templates |

## Planned Folder Contents (Phase-II)

```
aws/
├── cloudformation/     # Stack templates
├── lambda/             # Lambda function code
├── step-functions/     # Recovery workflow definitions
└── iam/                # IAM policies and roles
```

## Phase-I Status

No infrastructure deployed. AWS service planning is documented in `docs/AWS_Services_Planning.docx`.
