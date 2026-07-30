# Problem Statement — AORT

## Context

Banking institutions operate mission-critical **Enterprise Resource Planning (ERP)** systems on cloud infrastructure. Service continuity depends on cloud availability, transaction consistency, data recovery guarantees, and timely automated response — not financial risk modeling alone.

## Problem

Disaster recovery (DR) for banking ERP workloads on AWS remains largely:

1. **Reactive** — teams respond after outages rather than predicting and planning recovery proactively
2. **Fragmented** — financial stress testing, ERP control validation, BCM workflows, and AWS DR patterns exist in separate silos
3. **Static** — DR playbooks assume fixed RTO/RPO without comparing strategies under compound hazards
4. **Manual** — failover and restoration decisions require human intervention without AI-assisted ranking
5. **Non-integrated** — no unified system connects live telemetry, ERP dependencies, scenario simulation, and AWS execution

## Existing Challenges

| Challenge | Impact |
|-----------|--------|
| Multi-hazard disruptions (ransomware + region outage + surge load) | Compound failures exceed single-scenario playbook coverage |
| ERP-to-infrastructure dependency opacity | Recovery actions may restore infra but break transaction consistency |
| Compliance and audit requirements | Recovery decisions must be explainable and traceable |
| Cost vs resilience trade-offs | Over-provisioned DR vs under-protected critical services |
| Observability fragmentation | CloudWatch, CloudTrail, Config, and ERP logs analyzed separately |

## Proposed Solution

**AORT (Autonomous Operational Recovery Twin)** — an AI-driven AWS operational digital twin that:

- Continuously synchronizes AWS and ERP telemetry into a live operational twin
- Simulates multi-hazard banking disruption scenarios
- Predicts disruption propagation and recovery difficulty using AI
- Ranks recovery strategies against RTO, RPO, cost, compliance, and business impact
- Recommends AWS-native recovery actions through an administrator decision dashboard

## Motivation

- Banking downtime directly impacts customer trust, regulatory compliance, and revenue
- AWS provides resilience primitives (EDR, multi-region, Step Functions) but not intelligent strategy selection
- Digital twin and AI literature proves value in monitoring and simulation — but not yet for banking cloud DR optimization
- Academic and industry gap analysis confirms no unified AWS-native predictive DR framework for banking ERP

## Scope Boundaries (Phase-I)

| In Scope | Out of Scope (Phase-I) |
|----------|------------------------|
| Architecture and module design | Production deployment |
| Literature survey and gap analysis | Live banking data ingestion |
| Dataset planning (synthetic) | Full ML model training |
| AWS service mapping | Customer-facing banking application |
