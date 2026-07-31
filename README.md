# AORT: Autonomous Operational Recovery Twin

**An AI-Driven AWS Operational Digital Twin for Predictive Disaster Recovery Planning of Banking ERP Systems**

| | |
|---|---|
| **Course** | BCSE355L — Cloud Architecture Design |
| **Institution** | VIT University |
| **Instructor** | Dr. Priya V |
| **Repository** | AORT-AWS_Cloud_Project_2026 |
| **Phase** | Phase-I (Planning and Documentation) |

---

## Problem Statement

Banking institutions run mission-critical ERP systems on AWS. Disaster recovery today is largely **reactive**, **fragmented**, and **manual**:

- Financial stress testing, ERP control validation, business continuity workflows, and AWS DR patterns exist in separate silos
- DR playbooks assume fixed RTO/RPO without comparing strategies under compound hazards (ransomware + region outage + load surge)
- No unified system connects live telemetry, ERP dependencies, scenario simulation, and AWS recovery execution
- Recovery decisions in regulated banking must be explainable and auditable

**AORT** addresses this by proposing an AWS-native operational digital twin that predicts disruptions, simulates scenarios, ranks recovery strategies, and recommends AWS actions through a decision dashboard.

---

## Objectives

1. Develop an AWS-based operational digital twin mirroring banking infrastructure, ERP services, and dependencies in near real time
2. Build an AI prediction module identifying failure patterns from cloud telemetry and operational logs
3. Simulate at least five disaster scenarios: ransomware, server failure, network outage, regional failure, and transaction surge
4. Evaluate and rank recovery strategies using RTO, RPO, cost, service availability, and business continuity impact
5. Integrate AWS-native recovery mechanisms (Elastic DR, Step Functions, multi-region failover, AWS Backup)
6. Visualize resilience metrics, predicted impact, and recovery recommendations through a decision dashboard

---

## Novelty

AORT unifies four literature streams — financial/ERP digital twins, cyber resilience and BCM, AWS disaster recovery patterns, and operational digital twin monitoring — into one **predictive, decision-oriented framework**.

| Aspect | Contribution |
|--------|--------------|
| New feature | Predictive recovery planning instead of reactive monitoring |
| Better algorithm | AI-based ranking of recovery options vs fixed failover rules |
| Better architecture | Layered twin: infrastructure state + operational behavior + policy constraints |
| Better AWS integration | Native observability inputs and recovery execution (EDR, Step Functions) |
| Better security | Ransomware, compliance, and audit trail integration |
| Better automation | Simulate, compare, and recommend recovery actions with admin approval |

---

## Architecture

Two mandatory architecture diagrams are required for Phase-I (see `architecture/`).

### Diagram 1 — AWS Cloud Architecture

Shows AWS service interactions: Route 53, CloudFront, ALB, API Gateway, Lambda, EC2, ECS, RDS, S3, CloudWatch, CloudTrail, Config, IAM, SNS, SQS, Step Functions, AWS Backup, Elastic Disaster Recovery, VPC (public/private subnets), Digital Twin Engine, AI Decision Engine, and Recovery Optimizer.

### Diagram 2 — System Architecture

Shows AORT modules: Telemetry Collector, Operational Digital Twin, Dependency Graph Engine, Scenario Generator, AI Prediction Engine, Recovery Optimizer, AWS Recovery Orchestrator, Business Continuity Layer, Compliance Layer, and Decision Dashboard.

### Workflow

Normal operation → failure detection → telemetry collection → twin synchronization → scenario generation → AI prediction → recovery optimization → administrator approval → AWS recovery execution → post-recovery monitoring.

**Files:** `architecture/AWS_Architecture.png`, `architecture/System_Architecture.png`, `architecture/Workflow.png`

---

## AWS Services

| AWS Service | Purpose |
|-------------|---------|
| EC2, ECS, Lambda | Compute for twin engine, telemetry processing, ERP simulation |
| API Gateway, ALB, CloudFront, Route 53 | API and dashboard delivery |
| RDS, S3 | Twin state, logs, backups, model artifacts |
| CloudWatch, CloudTrail, Config | Monitoring, audit, compliance |
| IAM, Cognito, KMS | Authentication, authorization, encryption |
| SNS, SQS, Step Functions | Alerts, queuing, recovery orchestration |
| Elastic Disaster Recovery, AWS Backup | Disaster recovery execution |
| VPC, CloudFormation | Network isolation and infrastructure as code |

Full planning table: `docs/AWS_Services_Planning.docx`

---

## Dataset

| Dataset | Type | Purpose |
|---------|------|---------|
| Synthetic Banking ERP | Structured | ERP transactions and dependencies |
| CloudWatch Metrics | Time-series | Infrastructure health |
| CloudTrail Logs | Audit JSON | Security and API events |
| AWS Config Snapshots | Configuration | Compliance drift detection |
| ERP Transaction Logs | Structured logs | Business-process observability |
| Backup Metadata | JSON | RPO and recovery point tracking |
| Disaster Scenario Profiles | JSON | Multi-hazard simulation inputs |

Details: `dataset/dataset_description.pdf` and `docs/Dataset_Details.docx`

---

## Folder Structure

```
AORT-AWS_Cloud_Project_2026/
├── README.md
├── LICENSE
├── .gitignore
├── docs/
│   ├── Abstract.docx
│   ├── Literature_Survey.docx
│   ├── Research_Gap.docx
│   ├── Objectives.docx
│   ├── Novelty.docx
│   ├── AWS_Services_Planning.docx
│   ├── Dataset_Details.docx
│   └── Project_Report.docx
├── architecture/
│   ├── AWS_Architecture.png
│   ├── System_Architecture.png
│   └── Workflow.png
├── dataset/
│   ├── raw/
│   ├── processed/
│   └── dataset_description.pdf
├── src/
│   ├── frontend/
│   ├── backend/
│   ├── ml_model/
│   └── aws/
├── results/
└── presentation/
```

---

## Implementation Status

| Component | Phase-I | Phase-II |
|-----------|---------|----------|
| Documentation | In progress | Maintain |
| Architecture diagrams | Placeholder PNGs pending | Update as built |
| Literature survey | Draft in docs | — |
| Source code (`src/`) | Not started | Implement |
| Dataset (`raw/`, `processed/`) | Not started | Generate synthetic data |
| Results | Not started | After evaluation |
| AWS deployment | Not started | Deploy and test |

**Phase-I is planning only. No implementation code has been written.**

---

## Branch Strategy

```
main
 └── develop
      ├── feature/student1
      ├── feature/student2
      └── feature/student3
```

### Workflow

1. Each student works only on their own `feature/studentX` branch
2. Commit work regularly to the feature branch
3. Open a Pull Request from `feature/studentX` → `develop`
4. Team reviews and resolves conflicts
5. Merge approved changes into `develop`
6. When stable, merge `develop` → `main`

### Expected Activity (per student)

- 20–30 meaningful commits
- At least 2 Pull Requests
- Code review participation
- Regular weekly commits

---

## Student Contributions

| Activity | Student 1 | Student 2 | Student 3 |
|----------|:---------:|:---------:|:---------:|
| Literature Survey (5 papers) | Papers 1–5 | Papers 6–10 | Papers 11–15 |
| Research Gap (5 papers) | ✓ | ✓ | ✓ |
| AWS Architecture | ✓ | | |
| System Architecture | ✓ | ✓ | |
| Frontend (`src/frontend/`) | ✓ | | |
| Backend (`src/backend/`) | | ✓ | |
| ML Model (`src/ml_model/`) | | ✓ | |
| AWS Infrastructure (`src/aws/`) | ✓ | | ✓ |
| Dataset | | ✓ | ✓ |
| Documentation | ✓ | ✓ | ✓ |
| Testing | ✓ | ✓ | ✓ |
| Presentation | ✓ | ✓ | ✓ |
| GitHub Commits | ✓ | ✓ | ✓ |

---

## How to Run

Implementation has not started. This section will be updated in Phase-II.

```text
# Planned steps (Phase-II)
1. Clone repository and checkout develop
2. Configure AWS credentials (aws configure)
3. Deploy infrastructure (src/aws/cloudformation/)
4. Start backend services
5. Start frontend dashboard
6. Run ML inference pipeline
```

---

## Future Work

- Phase-II: Implement all six AORT modules on AWS
- Train and evaluate AI prediction and recovery ranking models
- Integrate chaos engineering and game-day feedback loop
- Multi-region active-active DR demonstration
- Compliance automation for banking regulations

---

## License

MIT License — see [LICENSE](LICENSE).

---

## References

Key literature streams: financial digital twins (Pattabhi, 2025), AI stress testing (Metha et al., 2025), ERP digital twin (Saadhu, 2026), BCM cyber resilience (Berg, 2024; Coiciu & Militaru, 2024), AWS banking DR (Ponnusamy, 2025), AWS FSI Lens (2026), Capital One resiliency patterns (2024). Full survey: `docs/Literature_Survey.docx`.
