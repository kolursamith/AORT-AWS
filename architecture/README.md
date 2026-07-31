# Architecture Diagrams

This folder contains the mandatory Phase-I architecture diagrams for the AORT project.

## Required Files

| File | Description |
|------|-------------|
| `AWS_Architecture.png` | **Diagram 1** — AWS Cloud Architecture showing how AWS services interact (data flow, storage, processing, authentication, monitoring, notifications) |
| `System_Architecture.png` | **Diagram 2** — Complete system architecture showing overall project workflow and all AORT modules |
| `Workflow.png` | End-to-end workflow: normal operation, failure detection, twin sync, scenario generation, AI prediction, recovery optimization, approval, and AWS execution |

## Diagram 1 — AWS Cloud Architecture

Must include:

- Users, Route 53, CloudFront, ALB, API Gateway
- Lambda, EC2, ECS, RDS, S3
- CloudWatch, CloudTrail, Config, IAM, SNS, SQS, Step Functions
- AWS Backup, Elastic Disaster Recovery
- VPC with public and private subnets
- Digital Twin Engine, AI Decision Engine, Recovery Optimizer

**Reference:** [AWS EV Digital Twin Solution](https://docs.aws.amazon.com/solutions/latest/ev-digital-twin-on-aws/solution-overview.html)

## Diagram 2 — System Architecture

Must include:

- User, Dashboard, Telemetry Collector
- Operational Digital Twin, Scenario Generator, Dependency Graph Engine
- AI Prediction Engine, Recovery Optimizer
- AWS Recovery Orchestrator
- Business Continuity Layer, Compliance Layer
- Decision Dashboard and Recovery Recommendations

## Workflow Diagram

Must illustrate:

1. Normal operation and telemetry collection
2. Failure detection
3. Digital twin synchronization
4. Scenario generation
5. AI prediction
6. Recovery optimization
7. Administrator approval
8. AWS recovery execution
9. Post-recovery monitoring

## Phase-I Status

Diagram PNG files are **to be exported** from your drawing tool (Draw.io, Lucidchart, or PowerPoint) and placed in this folder before final submission.
