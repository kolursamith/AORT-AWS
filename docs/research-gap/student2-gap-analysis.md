# Student 2 — Research Gap Analysis (Papers 9–15)

> **Student:** Student 2 | **Branch:** `feature/student2`  
> **Instruction:** Independent analysis — not copied from source papers.

---

## Paper 9: Shah (2025) — Real-World DR in AWS

| Aspect | Analysis |
|--------|----------|
| **Existing Method** | Literature synthesis of DR architectures targeting <1% downtime |
| **Advantages** | Quantifiable downtime targets and DR evolution timeline |
| **Limitations** | General enterprise focus; no ERP or digital twin integration |
| **Research Gap** | Downtime metrics not linked to predictive twin-based planning |
| **Improvement for AORT** | Use downtime targets as optimizer objective function constraints |

## Paper 10: AWS WAF — DR of Workloads on AWS (2026)

| Aspect | Analysis |
|--------|----------|
| **Existing Method** | Well-Architected Framework DR pillar; pattern taxonomy |
| **Advantages** | Standardized RTO/RPO classification across DR strategies |
| **Limitations** | Decision left to architect judgment |
| **Research Gap** | No automated pattern selection under compound hazards |
| **Improvement for AORT** | Map WAF patterns to optimizer strategy candidates with scored trade-offs |

## Paper 11: AWS EV Digital Twin Solution (2026)

| Aspect | Analysis |
|--------|----------|
| **Existing Method** | Reference architecture for telemetry + twin + AI monitoring |
| **Advantages** | Proven AWS-native twin ingestion and sync pattern |
| **Limitations** | EV domain; not banking ERP or DR optimization |
| **Research Gap** | Twin pattern exists but DR decision layer is absent |
| **Improvement for AORT** | Adapt ingestion/sync pattern; add recovery optimizer as new layer |

## Paper 12: Critical Infrastructure Digital Twin (Group D)

| Aspect | Analysis |
|--------|----------|
| **Existing Method** | Real-time monitoring, anomaly localization, prediction |
| **Advantages** | Demonstrates twin value for operational visibility |
| **Limitations** | Physical infrastructure focus; not cloud ERP |
| **Research Gap** | Monitoring without prescriptive recovery recommendation |
| **Improvement for AORT** | Apply anomaly localization to cloud service dependency graph |

## Paper 13: Post-Disaster Digital Twin (Group D)

| Aspect | Analysis |
|--------|----------|
| **Existing Method** | Recovery progress tracking under uncertainty |
| **Advantages** | Visibility into recovery state during incidents |
| **Limitations** | Post-hoc tracking; not predictive or AWS-orchestrated |
| **Research Gap** | Tracks recovery but doesn't optimize strategy beforehand |
| **Improvement for AORT** | Combine pre-incident prediction with post-incident feedback loop |

## Paper 14: Explainable AI for Resilience Decisions

| Aspect | Analysis |
|--------|----------|
| **Existing Method** | XAI techniques for operational decision justification |
| **Advantages** | Critical for regulated banking audit requirements |
| **Limitations** | Not integrated with cloud DR execution |
| **Research Gap** | Explainability needed but absent in DR automation literature |
| **Improvement for AORT** | Embed explainability in recovery optimizer ranking output |

## Paper 15: Multi-Region Resilience & Chaos Engineering

| Aspect | Analysis |
|--------|----------|
| **Existing Method** | Game days, chaos testing, observability-driven response |
| **Advantages** | Validates resilience under controlled failure |
| **Limitations** | Manual test design; not linked to AI scenario generator |
| **Research Gap** | Chaos experiments don't feed back into predictive twin |
| **Improvement for AORT** | Use chaos results as training data for optimizer feedback loop |

---

## Evaluation Metrics (Student 2 Responsibility)

| Metric | Description | Target |
|--------|-------------|--------|
| **RTO Accuracy** | Predicted vs actual recovery time | ±15% in simulation |
| **RPO Compliance** | Data loss within defined bounds | 100% compliance in tests |
| **Strategy Ranking Precision** | Top-ranked plan matches best outcome | ≥80% in simulation |
| **Anomaly Detection F1** | Failure prediction accuracy | ≥0.75 F1-score |
| **Explainability Coverage** | Recommendations with justification | 100% of ranked plans |
