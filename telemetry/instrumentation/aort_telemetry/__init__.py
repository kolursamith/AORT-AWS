"""OpenTelemetry instrumentation for the AORT banking workload generator.

This package lives in telemetry/ rather than banking/ so that Phase 2 keeps
working standalone: banking/ imports it lazily and degrades to a no-op when it
is not installed or when no OTLP endpoint is configured.

Activation is entirely by environment variable. If OTEL_EXPORTER_OTLP_ENDPOINT
is unset, every entry point here does nothing and the workload generator
behaves exactly as it did in Phase 2.

    export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
    python -m aort_workload --clients 5 --iterations 60

Everything emitted is derived from the generator's real observed call results.
No metric value is invented.
"""

from .otel import is_enabled, record_call, shutdown

__all__ = ["is_enabled", "record_call", "shutdown"]
__version__ = "0.1.0"
