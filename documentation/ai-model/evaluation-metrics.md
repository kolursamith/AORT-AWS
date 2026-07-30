# Evaluation Metrics — AORT

> **Owner:** Student 2

| Metric | Formula / Description | Target | Phase |
|--------|----------------------|--------|-------|
| **RTO Accuracy** | \|predicted_RTO - actual_RTO\| / actual_RTO | ≤ 15% error | Phase-II |
| **RPO Compliance** | Recovery points within defined bounds | 100% | Phase-II |
| **Ranking Precision@1** | Top-ranked plan matches best outcome | ≥ 80% | Phase-II |
| **Anomaly Detection F1** | Harmonic mean of precision and recall | ≥ 0.75 | Phase-II |
| **Twin Sync Latency** | Time between telemetry event and twin update | < 5 min | Phase-II |
| **Scenario Coverage** | Distinct hazard types simulated | ≥ 5 | Phase-I ✅ |
| **Explainability Rate** | Recommendations with justification | 100% | Phase-I ✅ |
| **Documentation Completeness** | Required Phase-I sections completed | 100% | Phase-I ✅ |

## Evaluation Workflow

```mermaid
flowchart LR
    Sim[Run Disaster Simulation] --> Measure[Collect RTO/RPO/Cost]
    Measure --> Compare[Compare Predicted vs Actual]
    Compare --> Score[Compute Metrics]
    Score --> Report[Results Report]
```
