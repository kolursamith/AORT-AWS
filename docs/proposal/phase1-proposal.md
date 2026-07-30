# Phase-I Proposal — AORT

> **Course:** BCSE355L — Cloud Architecture Design  
> **Instructor:** Dr. Priya V  
> **Submission Deadline:** 30 July 2026

## Abstract

Banking institutions operating Enterprise Resource Planning (ERP) systems on Amazon Web Services (AWS) face a critical challenge: disaster recovery remains largely reactive, fragmented across financial risk modeling, business continuity workflows, and static cloud architecture playbooks. Existing literature addresses financial digital twins, ERP control stress testing, cyber resilience management, and AWS disaster recovery patterns — but no unified framework integrates live telemetry, operational dependency modeling, multi-hazard scenario simulation, and AI-driven recovery optimization for banking cloud workloads.

This project proposes **AORT (Autonomous Operational Recovery Twin)** — an AI-driven AWS operational digital twin for predictive disaster recovery planning of banking ERP systems. AORT continuously ingests CloudWatch metrics, CloudTrail logs, AWS Config snapshots, and ERP transaction telemetry to maintain a near-real-time operational twin. A multi-hazard scenario generator models ransomware, server failures, network outages, regional failures, and transaction surges. An AI prediction engine forecasts disruption propagation, while a recovery optimizer ranks candidate strategies against RTO, RPO, cost, availability, and compliance metrics. Recommended actions map to AWS-native execution patterns including Elastic Disaster Recovery, Step Functions orchestration, multi-region failover, and AWS Backup.

The framework transforms disaster recovery from a reactive, manual process into a predictive, automated, and resilience-oriented system — addressing a documented research gap across banking risk, ERP resilience, business continuity, and AWS cloud recovery literature. Phase-I delivers complete architecture, literature survey, research gap analysis, dataset planning, and implementation roadmap. Phase-II will implement and evaluate the six-module AORT platform on AWS.

**AWS Services:** EC2, ECS, Lambda, API Gateway, RDS, S3, CloudWatch, CloudTrail, Config, IAM, Cognito, SNS, SQS, Step Functions, Elastic Disaster Recovery, AWS Backup, Route 53, CloudFront, ALB, VPC, KMS, CloudFormation.

---

## Methodology

1. **Literature Survey** — Analyze 15 Scopus-indexed papers (2023–2026) across four thematic groups
2. **Gap Analysis** — Independent per-student analysis identifying limitations and improvements
3. **Architecture Design** — Two mandatory diagrams: AWS Cloud Architecture and System Architecture
4. **Module Decomposition** — Six implementable components aligned with research gap findings
5. **Dataset Planning** — Synthetic banking ERP and AWS telemetry datasets
6. **Sprint-Based Roadmap** — Six sprints from foundation to Phase-I submission

## Expected Outcomes

- Complete AWS-native architecture for predictive banking DR
- Documented research contribution bridging four literature streams
- Synthetic dataset specification for twin and AI training
- Implementation roadmap for Phase-II development
- Decision dashboard design for recovery recommendation and approval

## Future Scope

- Phase-II implementation and AWS deployment
- SageMaker-based ML model training and evaluation
- Live chaos engineering integration with recovery feedback loop
- Multi-tenant banking DR-as-a-service extension
- Regulatory compliance automation (RBI, PCI-DSS alignment)
