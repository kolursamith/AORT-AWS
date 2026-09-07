# digital-twin/

**Owner: Owner A · Phase 4 — Digital twin · Status: not started**

The project's custom operational representation of the system: components,
dependencies, current state, scenario state, predicted impact and recovery
context.

The twin is **not** Grafana. Prometheus, Grafana and CloudWatch are
observability components; the twin is the project's own model that links
observations to topology and operational meaning.

Depends on the telemetry contract (Phase 3) and the contracts in
`contracts/`.
