# ML Model (Phase-II)

Planned AI/ML components for disruption prediction and recovery optimization.

## Planned Implementation

| Component | Description |
|-----------|-------------|
| Anomaly Detection | Identify failure patterns from CloudWatch and ERP telemetry |
| Propagation Model | Predict disruption spread across dependency graph |
| Impact Estimator | Forecast business impact and recovery difficulty |
| Recovery Ranker | Score DR strategies using RTO, RPO, cost, compliance features |
| Explainability Module | Generate justifications for ranked recovery recommendations |

## Planned Technology

- Python (scikit-learn, PyTorch, or TensorFlow)
- Amazon SageMaker for training and deployment (optional)
- Feature store in S3 / processed dataset folder

## Training Data

Synthetic banking ERP dataset, CloudWatch metrics, disaster scenario profiles (see `dataset/`).

## Phase-I Status

No code — planning only. Implementation begins in Phase-II.
