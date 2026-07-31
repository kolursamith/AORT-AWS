# Backend (Phase-II)

Planned server-side services for AORT core modules.

## Planned Implementation

| Module | Description |
|--------|-------------|
| Telemetry Collector API | Ingest and normalize CloudWatch, CloudTrail, Config, ERP logs |
| Digital Twin Service | Maintain and query operational twin state |
| Scenario Generator API | Create and manage disaster scenario profiles |
| Dependency Graph Service | Build and update service topology graph |
| Recovery Optimizer API | Score and rank candidate DR strategies |
| Orchestrator Interface | Submit approved plans to AWS Step Functions |

## Planned Technology

- Python or Node.js REST services
- Amazon API Gateway + Lambda / ECS
- Amazon RDS for twin state and metadata
- Amazon SQS for async telemetry pipeline

## Phase-I Status

No code — planning only. Implementation begins in Phase-II.
