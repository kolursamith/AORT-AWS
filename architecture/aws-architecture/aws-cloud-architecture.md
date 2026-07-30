# AWS Cloud Architecture — Diagram 1

> **Reference:** [AWS EV Digital Twin with AI-Powered Operational Monitoring](https://docs.aws.amazon.com/solutions/latest/ev-digital-twin-on-aws/solution-overview.html)  
> **Phase-I:** Planning architecture — no infrastructure deployed

## Architecture Overview

This diagram shows how AWS services interact to support AORT's telemetry ingestion, digital twin engine, AI decision engine, recovery orchestration, monitoring, security, and notifications.

```mermaid
flowchart TB
    subgraph Users["Users & Operators"]
        Admin[Banking DR Administrator]
        Auditor[Compliance Auditor]
        Dashboard[Decision Dashboard]
    end

    subgraph Edge["Edge & Routing"]
        R53[Amazon Route 53]
        CF[Amazon CloudFront]
        ALB[Application Load Balancer]
        APIGW[Amazon API Gateway]
    end

    subgraph VPC["Amazon VPC"]
        subgraph Public["Public Subnet"]
            NAT[NAT Gateway]
            Bastion[Bastion / Jump Host]
        end
        subgraph Private["Private Subnet"]
            ECS[Amazon ECS<br/>Digital Twin Engine]
            EC2[Amazon EC2<br/>ERP Simulator / Twin Workers]
            Lambda[AWS Lambda<br/>Telemetry Processor]
            SF[AWS Step Functions<br/>Recovery Orchestrator]
        end
    end

    subgraph Data["Data & Storage"]
        RDS[(Amazon RDS<br/>ERP Metadata & Twin State)]
        S3[(Amazon S3<br/>Logs, Backups, Models)]
        EDR[AWS Elastic Disaster Recovery]
        Backup[AWS Backup]
    end

    subgraph AI["AI & Decision"]
        Twin[Operational Digital Twin]
        AIEngine[AI Decision Engine]
        Optimizer[Recovery Optimizer]
    end

    subgraph Messaging["Messaging"]
        SNS[Amazon SNS]
        SQS[Amazon SQS]
    end

    subgraph Security["Security & Governance"]
        IAM[AWS IAM]
        Cognito[Amazon Cognito]
        KMS[AWS KMS]
    end

    subgraph Observability["Monitoring & Audit"]
        CW[Amazon CloudWatch]
        CT[AWS CloudTrail]
        Config[AWS Config]
    end

  Admin --> R53
  Auditor --> R53
  R53 --> CF
  CF --> ALB
  ALB --> APIGW
  APIGW --> Lambda
  APIGW --> ECS
  Dashboard --> CF

  Lambda --> SQS
  SQS --> Twin
  ECS --> Twin
  EC2 --> Twin
  Twin --> AIEngine
  AIEngine --> Optimizer
  Optimizer --> SF

  SF --> EDR
  SF --> Backup
  SF --> RDS
  SF --> ECS

  Lambda --> RDS
  Twin --> RDS
  Lambda --> S3
  CT --> S3
  Config --> S3
  CW --> S3

  Twin --> CW
  SF --> CW
  Optimizer --> SNS
  SNS --> Admin

  IAM --> APIGW
  Cognito --> APIGW
  IAM --> ECS
  IAM --> Lambda
  KMS --> RDS
  KMS --> S3

  CT --> Auditor
  Config --> Auditor
```

---

## Data Flow

```mermaid
sequenceDiagram
    participant ERP as ERP / Banking Workloads
    participant CW as CloudWatch
    participant CT as CloudTrail
    participant CFG as AWS Config
    participant L as Lambda Collector
    participant Q as SQS
    participant DT as Digital Twin Engine
    participant AI as AI Decision Engine
    participant RO as Recovery Optimizer
    participant SF as Step Functions
    participant SNS as SNS Alerts

    ERP->>CW: Metrics & health signals
    ERP->>CT: API / audit events
    CFG->>L: Configuration snapshots
    CW->>L: Metric streams
    CT->>L: Log events
    L->>Q: Normalized telemetry
    Q->>DT: Twin state update
    DT->>AI: Dependency graph + state
    AI->>RO: Predicted disruption profile
    RO->>SF: Ranked recovery plan
    SF->>SNS: Execution status / approval request
```

---

## Service Responsibilities

| Layer | AWS Services | Responsibility |
|-------|-------------|----------------|
| **Ingress** | Route 53, CloudFront, ALB, API Gateway | Secure user access, API routing, dashboard delivery |
| **Compute** | ECS, EC2, Lambda | Twin engine, telemetry processing, ERP simulation |
| **Orchestration** | Step Functions, SQS | Recovery workflow execution, async telemetry pipeline |
| **Data** | RDS, S3, Backup, Elastic DR | Twin state, logs, backups, cross-region recovery |
| **AI** | Lambda + ECS (SageMaker Phase-II) | Prediction and recovery optimization |
| **Security** | IAM, Cognito, KMS | Authentication, authorization, encryption |
| **Observability** | CloudWatch, CloudTrail, Config | Monitoring, audit, compliance evidence |
| **Notifications** | SNS | Alerts to administrators |

---

## Authentication Flow

```mermaid
flowchart LR
    User[Administrator] --> Cognito[Amazon Cognito]
    Cognito --> IAM[IAM Role Assignment]
    IAM --> APIGW[API Gateway]
    APIGW --> AuthZ{Authorized?}
    AuthZ -->|Yes| Services[AORT Services]
    AuthZ -->|No| Deny[403 Denied]
    Services --> CT[CloudTrail Audit Log]
```

---

## Subnet Design

| Subnet | Resources | Rationale |
|--------|-----------|-----------|
| **Public** | ALB, NAT Gateway, Bastion | Internet-facing entry; controlled admin access |
| **Private** | ECS, EC2, Lambda, RDS, Twin Engine | No direct internet; defense in depth |

---

## Multi-Region Consideration

Primary region hosts active twin and ERP simulator. Secondary region holds EDR replicas and warm standby resources for regional failure scenarios (Phase-II).
