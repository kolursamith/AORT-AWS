# Architecture

## Trust and data-flow boundaries

```mermaid
flowchart TB
  subgraph Synthetic payment plane
    RM[Remitter API] --> SW[Switch ingress]
    SW --> BF[Beneficiary mock]
    SW --> DB[(PostgreSQL ledger)]
    DB --> OB[Transactional outbox]
  end
  subgraph Observation plane
    OB --> TE[Transaction events]
    SW --> RT[Route telemetry]
    TE --> TT[Transaction twin]
    RT --> DT[Route twin]
  end
  subgraph Decision plane
    TT --> OP[Look-ahead and ranking]
    DT --> OP
    OP --> SG[Invariant / confidence gate]
    SG --> RD[RecoveryDecision]
  end
  RD -->|budget and IDs, never balances| SW
```

The payment plane owns financial truth. The observation plane contains delayed and possibly hostile evidence. The decision plane is advisory. Cross-plane communication uses `contracts/v1` and documented HTTP APIs.

## Local deployment

Compose starts PostgreSQL and isolated controller, switch, remitter, and beneficiary containers. The intelligence simulator and controller can run without any banking service. The banking APIs default to fixed credits and need no ML model.

## AWS evolution

The MVP maps containers to one controlled EC2 host. The course design maps stateless services to ECS, PostgreSQL to RDS, check dispatch to SQS, and logs/metrics to CloudWatch. S3 holds synthetic experiment artifacts. DynamoDB is omitted unless a measured twin access pattern justifies it.
