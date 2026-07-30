# Recovery Orchestration — Sequence Diagram

```mermaid
sequenceDiagram
    autonumber
    participant Admin as Administrator
    participant Dash as Decision Dashboard
    participant Twin as Operational Digital Twin
    participant AI as AI Prediction Engine
    participant Opt as Recovery Optimizer
    participant Comp as Compliance Layer
    participant SF as Step Functions
    participant EDR as Elastic DR
    participant SNS as SNS

    Note over Twin: Failure detected via telemetry
    Twin->>AI: Current state + dependency graph
    AI->>Opt: Disruption prediction report
    Opt->>Opt: Generate & rank DR plans
    Opt->>Dash: Display ranked recommendations
    Dash->>Admin: Present RTO/RPO/cost comparison
    Admin->>Comp: Request policy validation
    Comp->>Admin: Compliance approved
    Admin->>SF: Approve Plan #1 (Warm Standby Failover)
    SF->>EDR: Initiate failover workflow
    EDR->>SF: Recovery instance launched
    SF->>Twin: Update twin state
    SF->>SNS: Recovery complete notification
    SNS->>Admin: Alert delivered
    Twin->>AI: Post-recovery feedback data
```
