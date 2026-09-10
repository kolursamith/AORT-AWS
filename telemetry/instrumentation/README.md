# aort-telemetry

OpenTelemetry instrumentation for the Phase 2 banking workload generator.

Installed into the same environment as `aort_workload`:

```bash
pip install -e telemetry/instrumentation
```

It is inert unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set, so the Phase 2
generator and its verification script behave identically without it.
