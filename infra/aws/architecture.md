# AWS target architecture

```mermaid
flowchart LR
  U[Demo client] --> ALB[ALB / restricted ingress]
  ALB --> ECS[ECS services]
  ECS --> RDS[(RDS PostgreSQL ledger)]
  ECS --> SQS[SQS status-check queue]
  ECS --> CW[CloudWatch logs and metrics]
  ECS --> S3[(S3 synthetic results)]
  SQS --> ECS
```

RDS is the only proposed financial source of truth. SQS and any twin store are projections or delivery mechanisms, not participants in a fictional cross-database atomic transaction.
