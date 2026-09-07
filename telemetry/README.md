# telemetry/

**Owner: Owner B · Phase 3 — Telemetry validation · Status: not started**

Collects application, infrastructure and database observations and normalizes
them into the project's telemetry contract.

Planned: OpenTelemetry instrumentation, a Prometheus scrape configuration, and
exporters for container and PostgreSQL metrics.

Starting point: `banking/` already exposes Fineract's Prometheus actuator
endpoint at `/fineract-provider/actuator/prometheus` (~99KB of metrics,
confirmed live). Nothing consumes it yet.

The blueprint is explicit that the telemetry surface must be **inspected on the
running system before** any schema is finalized. Do not write a contract for
fields that have not been observed.
