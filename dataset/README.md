# Dataset

Phase-I defines dataset planning only. No real banking customer data is used.

## Folder Structure

```
dataset/
├── raw/                  # Unprocessed telemetry and log exports
├── processed/            # Cleaned, feature-ready datasets (Phase-II)
└── dataset_description.pdf   # Full dataset specification document
```

## Planned Datasets

| Dataset | Source | Purpose |
|---------|--------|---------|
| Synthetic Banking ERP Dataset | Generated | ERP transactions and service dependencies |
| CloudWatch Metrics | AWS (simulated) | Infrastructure health and custom ERP metrics |
| CloudTrail Logs | AWS (simulated) | API audit and security events |
| AWS Config Snapshots | AWS (simulated) | Configuration compliance and drift |
| ERP Transaction Logs | ERP simulator | Business-process observability |
| Backup Metadata | AWS Backup (simulated) | RPO and recovery point availability |
| Disaster Scenario Profiles | Scenario generator | Multi-hazard simulation inputs |

## Dataset Details Document

Complete specifications (name, source, URL, size, records, features, data type, license, preprocessing) are documented in:

- `dataset_description.pdf`
- `docs/Dataset_Details.docx`

## Phase-I Status

- `raw/` and `processed/` are empty placeholders for Phase-II
- Replace `dataset_description.pdf` with the final PDF before submission
