# Project Workflows — AORT

## 1. Normal Operation Workflow

```mermaid
flowchart TD
    A[Banking ERP Services Running] --> B[CloudWatch / CloudTrail / Config Collect Metrics]
    B --> C[Telemetry Collector Ingests Data]
    C --> D[Operational Digital Twin Updates State]
    D --> E[Dependency Graph Refreshed]
    E --> F[Dashboard Shows Healthy Status]
    F --> G[Continuous Monitoring Loop]
    G --> B
```

---

## 2. Failure Detection Workflow

```mermaid
flowchart TD
    A[Anomaly in Metrics / Logs] --> B{AI Prediction Engine}
    B -->|Threshold Breach| C[Flag Potential Disruption]
    B -->|Normal Variance| D[Continue Monitoring]
    C --> E[Classify Failure Type]
    E --> F[Update Twin State]
    F --> G[Trigger Scenario Analysis]
    G --> H[Notify via SNS]
```

---

## 3. Telemetry Collection Workflow

```mermaid
sequenceDiagram
    participant AWS as AWS Services
    participant ERP as ERP Simulator
    participant TC as Telemetry Collector
    participant SQS as Amazon SQS
    participant S3 as Amazon S3
    participant DT as Digital Twin

    AWS->>TC: CloudWatch metrics
    AWS->>TC: CloudTrail events
    AWS->>TC: Config snapshots
    ERP->>TC: Transaction & health logs
    TC->>SQS: Enqueue normalized events
    SQS->>DT: Process batch updates
    TC->>S3: Archive raw telemetry
    DT->>DT: Update twin graph
```

---

## 4. Digital Twin Synchronization Workflow

```mermaid
flowchart LR
    Raw[Raw Telemetry] --> Norm[Normalization]
    Norm --> Map[Service Mapping]
    Map --> Graph[Dependency Graph Update]
    Graph --> State[Twin State Store RDS]
    State --> Validate[Consistency Check]
    Validate --> Publish[Publish to Dashboard]
```

---

## 5. Scenario Generation Workflow

```mermaid
flowchart TD
    A[Select Hazard Type] --> B[Load Base Twin State]
    B --> C[Inject Disruption Parameters]
    C --> D[Propagate via Dependency Graph]
    D --> E[Generate Compound Scenario]
    E --> F[Store Scenario Profile]
    F --> G[Pass to AI Prediction Engine]
```

**Supported Scenarios:** Ransomware, server crash, network outage, regional failure, transaction surge, insider/config drift

---

## 6. AI Prediction Workflow

```mermaid
flowchart TD
    A[Twin State + Scenario] --> B[Feature Extraction]
    B --> C[Anomaly Detection Model]
    B --> D[Propagation Graph Model]
    C --> E[Disruption Severity Score]
    D --> F[Affected Service Set]
    E --> G[Business Impact Estimate]
    F --> G
    G --> H[Prediction Report to Optimizer]
```

---

## 7. Recovery Optimization Workflow

```mermaid
flowchart TD
    A[Prediction Report] --> B[Generate Candidate DR Plans]
    B --> C[Plan 1: Backup Restore]
    B --> D[Plan 2: Warm Standby Failover]
    B --> E[Plan 3: Active-Active Route]
    B --> F[Plan N: EDR Launch]
    C --> G[Score: RTO RPO Cost Compliance]
    D --> G
    E --> G
    F --> G
    G --> H[Rank Plans]
    H --> I[Generate Explainable Recommendation]
```

---

## 8. Recommendation & Administrator Approval Workflow

```mermaid
sequenceDiagram
    participant RO as Recovery Optimizer
    participant DD as Decision Dashboard
    participant Admin as Administrator
    participant CL as Compliance Layer
    participant ARD as AWS Recovery Orchestrator

    RO->>DD: Present ranked recovery plans
    DD->>Admin: Show RTO/RPO/cost/impact comparison
    Admin->>CL: Compliance check
    CL->>Admin: Policy validation result
    Admin->>ARD: Approve selected plan
    ARD->>ARD: Execute recovery workflow
```

---

## 9. AWS Recovery Execution Workflow

```mermaid
flowchart TD
    A[Approved Recovery Plan] --> B[Step Functions Workflow Start]
    B --> C{Strategy Type}
    C -->|Backup Restore| D[S3/RDS Snapshot Restore]
    C -->|Failover| E[Route 53 + ALB Switch]
    C -->|EDR| F[Elastic DR Instance Launch]
    D --> G[Validate Service Health]
    E --> G
    F --> G
    G --> H{Recovery Successful?}
    H -->|Yes| I[Update Twin State]
    H -->|No| J[Escalate / Retry / Fallback Plan]
    I --> K[SNS Success Notification]
    J --> K
```

---

## 10. Post-Recovery Monitoring Workflow

```mermaid
flowchart TD
    A[Recovery Complete] --> B[Enhanced CloudWatch Monitoring]
    B --> C[Twin Re-Synchronization]
    C --> D[Compare Predicted vs Actual RTO/RPO]
    D --> E[Feedback to AI Optimizer]
    E --> F[Update Model Weights / Rankings]
    F --> G[Generate Post-Incident Report]
    G --> H[Archive to S3 + Audit Trail]
```

---

## End-to-End Master Workflow

```mermaid
flowchart TB
    Start([Normal Operations]) --> Monitor[Continuous Telemetry]
    Monitor --> Detect{Failure Detected?}
    Detect -->|No| Monitor
    Detect -->|Yes| Twin[Update Digital Twin]
    Twin --> Scenario[Generate Scenarios]
    Scenario --> Predict[AI Prediction]
    Predict --> Optimize[Rank Recovery Plans]
    Optimize --> Approve{Admin Approval}
    Approve -->|Rejected| Optimize
    Approve -->|Approved| Execute[AWS Recovery Orchestration]
    Execute --> PostMonitor[Post-Recovery Monitoring]
    PostMonitor --> Learn[Feedback Loop]
    Learn --> Monitor
```
