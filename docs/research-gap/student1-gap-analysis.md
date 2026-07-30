# Student 1 — Research Gap Analysis (Papers 1–8)

> **Student:** Student 1 | **Branch:** `feature/student1`  
> **Instruction:** Independent analysis — not copied from source papers.

---

## Paper 1: Pattabhi (2025) — Financial Digital Twins

| Aspect | Analysis |
|--------|----------|
| **Existing Method** | Literature synthesis of AI/ML, Monte Carlo, and agent-based modeling for banking risk |
| **Advantages** | Establishes conceptual foundation for financial digital twins and stress testing |
| **Limitations** | Stops at financial analytics; no infrastructure or cloud recovery dimension |
| **Research Gap** | Cannot answer "which AWS action restores ERP service fastest?" from financial twin alone |
| **Improvement for AORT** | Extend twin schema with cloud resource nodes and DR state attributes |

## Paper 2: Metha et al. (2025) — AI-Driven Financial Stress Testing

| Aspect | Analysis |
|--------|----------|
| **Existing Method** | LSTM, GAN, XGBoost, DRL for economic disruption simulation |
| **Advantages** | Strong dynamic scenario generation and multi-signal risk coverage |
| **Limitations** | Economic focus; no mapping to infrastructure recovery execution |
| **Research Gap** | Scenarios predict financial shock but not operational recovery path |
| **Improvement for AORT** | Repurpose scenario engine for infrastructure hazards (ransomware, outage) |

## Paper 3: Saadhu (2026) — ERP Digital Twin Stress Testing

| Aspect | Analysis |
|--------|----------|
| **Existing Method** | ERP control modeling, backtesting, adversarial stress scenarios |
| **Advantages** | Directly applicable ERP process and control validation |
| **Limitations** | Enterprise-control domain only; no cloud topology |
| **Research Gap** | ERP twin lacks AWS regional failover and backup state awareness |
| **Improvement for AORT** | Fuse ERP control graph with AWS service dependency graph |

## Paper 4: van den Berg (2024) — Resilience Digital Model

| Aspect | Analysis |
|--------|----------|
| **Existing Method** | Conceptual digital model integrating DR testing and BIA |
| **Advantages** | Bridges continuity processes with interdependency visibility |
| **Limitations** | Not implemented; not AI-driven or cloud-connected |
| **Research Gap** | Model exists on paper but cannot adapt to live telemetry |
| **Improvement for AORT** | Operationalize as AWS-connected twin with continuous sync |

## Paper 5: Coiciu & Militaru (2024) — Digital BCM System

| Aspect | Analysis |
|--------|----------|
| **Existing Method** | BCM software with ransomware simulation |
| **Advantages** | Practical evidence for digitized recovery planning |
| **Limitations** | Workflow tool, not predictive optimizer |
| **Research Gap** | Plans are documented but not ranked by predicted outcome |
| **Improvement for AORT** | Add AI scoring layer on top of BCM workflow outputs |

## Paper 6: Ponnusamy (2025) — Banking DR on AWS

| Aspect | Analysis |
|--------|----------|
| **Existing Method** | Comparative analysis of AWS DR services and patterns |
| **Advantages** | Clear mapping of AWS services to banking DR needs |
| **Limitations** | Descriptive; no decision intelligence |
| **Research Gap** | Describes options but not which to choose per scenario |
| **Improvement for AORT** | Use as execution catalog for recovery optimizer output |

## Paper 7: AWS FSI Industry Lens (2026)

| Aspect | Analysis |
|--------|----------|
| **Existing Method** | Architecture guidance for resilient financial workloads |
| **Advantages** | Authoritative compliance and resilience constraints |
| **Limitations** | Static guidance document |
| **Research Gap** | Constraints known but not dynamically enforced in recovery decisions |
| **Improvement for AORT** | Encode FSI lens rules as compliance layer constraints in optimizer |

## Paper 8: Capital One Cloud Resiliency (2024)

| Aspect | Analysis |
|--------|----------|
| **Existing Method** | Case study: multi-region, active/active, chaos engineering |
| **Advantages** | Proven patterns at financial services scale |
| **Limitations** | Engineering narrative; no formal optimization |
| **Research Gap** | Patterns demonstrated but selection logic not automated |
| **Improvement for AORT** | Convert patterns into optimizer strategy templates |
