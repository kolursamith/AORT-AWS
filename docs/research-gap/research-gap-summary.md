# Research Gap Analysis — Summary

> **Primary Source:** Combined Research Gap Analysis Report  
> **Project:** Predictive Disaster Recovery Planning Framework for Banking Infrastructure using Digital Twin Simulation

## Executive Summary

Existing research addresses **financial stress testing**, **ERP control validation**, **business continuity management**, **AWS disaster recovery patterns**, and **operational digital twins** — but only in isolation. None provides a unified, AWS-native, AI-driven framework that:

1. Continuously ingests banking and cloud telemetry
2. Maintains a live operational digital twin of ERP dependencies
3. Simulates compound multi-hazard disruption scenarios
4. Ranks recovery strategies using RTO, RPO, cost, compliance, and business impact
5. Maps recommendations to executable AWS recovery orchestration

---

## Gap by Literature Group

### Group A: Banking Risk, ERP, and Financial Stress

| Aspect | Finding |
|--------|---------|
| **Existing Methods** | AI/ML stress testing, Monte Carlo simulation, ERP control digital twins |
| **Advantages** | Strong financial and ERP process modeling |
| **Limitations** | No AWS infrastructure behavior, failover decisions, or DR orchestration |
| **Research Gap** | Banking resilience requires cloud availability, transaction consistency, and automated infrastructure response — not financial prediction alone |
| **Improvement** | Extend feedback-driven ERP twin with infrastructure state and DR metrics (RTO/RPO) |

### Group B: Cyber Resilience and Business Continuity

| Aspect | Finding |
|--------|---------|
| **Existing Methods** | BCM systems, BIA, ransomware simulation, resilience digital models |
| **Advantages** | Structured continuity planning and cyber incident workflows |
| **Limitations** | Procedural recovery; no real-time AI comparison of strategies |
| **Research Gap** | Recovery treated as workflow, not intelligent optimization |
| **Improvement** | Integrate BIA and recovery modeling into twin for business-aware technical decisions |

### Group C: Cloud and AWS Disaster Recovery

| Aspect | Finding |
|--------|---------|
| **Existing Methods** | Multi-region failover, backup strategies, DR pattern catalogs |
| **Advantages** | Practical AWS-native resilience building blocks |
| **Limitations** | Architecture guidance, not predictive adaptive systems |
| **Research Gap** | Building blocks exist without intelligence to select best action per scenario |
| **Improvement** | Use AWS patterns as execution layer beneath AI recovery decision engine |

### Group D: Critical Infrastructure Digital Twins

| Aspect | Finding |
|--------|---------|
| **Existing Methods** | Real-time monitoring, anomaly localization, post-disaster tracking |
| **Advantages** | Twin visibility into recovery progress and explainability |
| **Limitations** | Generic or physical-infra focus; not banking cloud ERP |
| **Research Gap** | Need banking operational twins understanding topology, transaction state, compliance |
| **Improvement** | Adapt explainability and state tracking to infrastructure telemetry |

---

## Synthesized Research Gap Statement

> Existing studies either model financial stress, validate ERP controls, or describe AWS resilience patterns, but **none integrates these capabilities** into an adaptive banking digital twin that intelligently selects the best cloud recovery strategy under compound disruptions.

## Individual Analysis Files

| Student | File | Papers |
|---------|------|--------|
| Student 1 | [student1-gap-analysis.md](student1-gap-analysis.md) | 1–8 |
| Student 2 | [student2-gap-analysis.md](student2-gap-analysis.md) | 9–15 |

> **Important:** Each student prepares independent gap analysis reflecting their own understanding. Do not copy gaps directly from source papers.
