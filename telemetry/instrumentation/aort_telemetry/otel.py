"""OTLP traces and metrics for the banking workload generator.

Emits, per Fineract call the generator actually made:

  * a span, reconstructed with the real start and end timestamps
  * a counter of operations, split by operation, category and outcome
  * a duration histogram
  * a counter of HTTP status codes

Every value comes from an OperationResult the generator produced from a real
HTTP response. Nothing here fabricates data: if the generator made no calls,
these series stay empty.
"""

from __future__ import annotations

import atexit
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Populated by _setup() on first use; stay None when instrumentation is off.
_tracer = None
_meter_provider = None
_tracer_provider = None
_op_counter = None
_duration_histogram = None
_status_counter = None
_initialised = False
_enabled = False

SERVICE_NAME = os.getenv("AORT_TELEMETRY_SERVICE_NAME", "aort-workload-generator")


def _setup() -> bool:
    """Initialise OTLP export. Returns whether instrumentation is active.

    Activation requires OTEL_EXPORTER_OTLP_ENDPOINT to be set. Any import or
    connection problem disables instrumentation rather than breaking the
    workload run - telemetry must never take the banking workload down.
    """
    global _tracer, _meter_provider, _tracer_provider, _initialised, _enabled
    global _op_counter, _duration_histogram, _status_counter

    if _initialised:
        return _enabled
    _initialised = True

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        logger.debug("OTEL_EXPORTER_OTLP_ENDPOINT unset; telemetry disabled.")
        _enabled = False
        return False

    try:
        from opentelemetry import metrics, trace
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:
        logger.warning("OpenTelemetry SDK not installed (%s); telemetry disabled.", exc)
        _enabled = False
        return False

    try:
        resource = Resource.create(
            {
                "service.name": SERVICE_NAME,
                "service.version": os.getenv("AORT_TELEMETRY_VERSION", "0.1.0"),
                "deployment.environment": os.getenv(
                    "AORT_TELEMETRY_ENVIRONMENT", "local"
                ),
            }
        )

        _tracer_provider = TracerProvider(resource=resource)
        _tracer_provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"))
        )
        trace.set_tracer_provider(_tracer_provider)
        _tracer = trace.get_tracer("aort.workload")

        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics"),
            export_interval_millis=5000,
        )
        _meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
        metrics.set_meter_provider(_meter_provider)
        meter = metrics.get_meter("aort.workload")

        # Counter names deliberately omit _total: the Prometheus exporter
        # appends that suffix itself.
        _op_counter = meter.create_counter(
            "aort_workload_operations",
            unit="1",
            description="Banking operations attempted against Fineract",
        )
        _duration_histogram = meter.create_histogram(
            "aort_workload_operation_duration",
            unit="s",
            description="Latency of Fineract API calls made by the workload generator",
        )
        _status_counter = meter.create_counter(
            "aort_workload_http_responses",
            unit="1",
            description="HTTP status codes returned by Fineract",
        )

        atexit.register(shutdown)
        _enabled = True
        logger.info("AORT telemetry enabled, exporting OTLP to %s", endpoint)
    except Exception as exc:  # noqa: BLE001 - never break the workload run
        logger.warning("Telemetry setup failed (%s); continuing without it.", exc)
        _enabled = False

    return _enabled


def is_enabled() -> bool:
    """Whether instrumentation is active for this process."""
    return _setup()


def record_call(result: Any, started_ns: int | None = None) -> None:
    """Record one completed Fineract call.

    `result` is a banking workload OperationResult. `started_ns` is the wall
    clock time the request began, in nanoseconds, used to reconstruct the span
    with its true start and end. Safe to call when instrumentation is off.
    """
    if not _setup():
        return

    try:
        attributes = {
            "aort.operation": result.operation,
            "aort.category": result.category,
            "http.request.method": result.method,
            "http.route": result.path,
            "aort.outcome": "ok" if result.ok else "error",
        }
        if result.http_status is not None:
            attributes["http.response.status_code"] = int(result.http_status)

        _op_counter.add(1, attributes)
        _duration_histogram.record(
            result.latency_ms / 1000.0,
            {
                "aort.operation": result.operation,
                "aort.category": result.category,
                "aort.outcome": "ok" if result.ok else "error",
            },
        )
        _status_counter.add(
            1,
            {
                "aort.operation": result.operation,
                "http.response.status_code": str(result.http_status),
            },
        )
        _emit_span(result, attributes, started_ns)
    except Exception as exc:  # noqa: BLE001 - telemetry must not break the run
        logger.debug("Telemetry record_call failed: %s", exc)


def _emit_span(result: Any, attributes: dict, started_ns: int | None) -> None:
    """Reconstruct the call as a span using its real start and end times."""
    if started_ns is None:
        return

    from opentelemetry.trace import Status, StatusCode

    end_ns = started_ns + int(result.latency_ms * 1_000_000)
    span = _tracer.start_span(
        result.operation,
        start_time=started_ns,
        attributes=attributes,
    )
    if result.ok:
        span.set_status(Status(StatusCode.OK))
    else:
        span.set_status(Status(StatusCode.ERROR, result.error or "call failed"))
    span.end(end_time=end_ns)


def shutdown() -> None:
    """Flush pending spans and metrics. Registered with atexit."""
    global _tracer_provider, _meter_provider
    for provider in (_tracer_provider, _meter_provider):
        if provider is None:
            continue
        try:
            provider.shutdown()
        except Exception:  # noqa: BLE001
            pass
    _tracer_provider = None
    _meter_provider = None
