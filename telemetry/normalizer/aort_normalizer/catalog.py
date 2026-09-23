"""Declarative catalog of the banking-system signals Layer 1a normalizes.

Every query targets a metric that was observed live in Phase 3 (see
contracts/telemetry-observed-0.2-proposed.md). Each ungrouped query aggregates
to at most one series; a query that ever returns more is reported as
``ambiguous`` rather than guessed at.

Design notes:

* ``sum(...)`` over no series is an empty result in PromQL, not zero, so a
  vanished component surfaces as ``missing`` instead of a fabricated 0.
* Fineract request signals exclude ``/actuator`` URIs so that Prometheus's own
  scraping is not counted as banking traffic.
* ``X or (Y * 0)`` yields 0 only while the component is serving traffic but has
  no errors, and stays empty when the component itself is gone.
"""

from __future__ import annotations

from dataclasses import dataclass

NON_NEGATIVE: tuple[float | None, float | None] = (0.0, None)
UNIT_INTERVAL: tuple[float | None, float | None] = (0.0, 1.0)


@dataclass(frozen=True)
class SignalSpec:
    component_id: str
    signal: str
    category: str
    unit: str
    promql: str
    source: str
    group_by: tuple[str, ...] = ()
    bounds: tuple[float | None, float | None] = NON_NEGATIVE


# --- reusable query fragments -------------------------------------------------

_FINERACT_HTTP = 'http_server_requests_seconds_count{job="fineract",uri!~".*actuator.*"}'
_FINERACT_HTTP_SUM = 'http_server_requests_seconds_sum{job="fineract",uri!~".*actuator.*"}'
_FINERACT_HTTP_5XX = (
    'http_server_requests_seconds_count{job="fineract",uri!~".*actuator.*",'
    'outcome="SERVER_ERROR"}'
)
_PG_DB = 'datname="fineract_default"'
_OPS = "aort_workload_operations_total"


def _container_cpu(name: str) -> str:
    return f'sum(rate(container_cpu_usage_seconds_total{{name="{name}"}}[1m]))'


def _container_memory(name: str) -> str:
    return f'sum(container_memory_usage_bytes{{name="{name}"}})'


CATALOG: tuple[SignalSpec, ...] = (
    # ---- fineract: the core-banking application --------------------------------
    SignalSpec("fineract", "up", "application", "boolean",
               'max(up{job="fineract"})', "prometheus:job=fineract",
               bounds=UNIT_INTERVAL),
    SignalSpec("fineract", "http_request_rate", "application", "requests_per_second",
               f"sum(rate({_FINERACT_HTTP}[1m]))", "prometheus:job=fineract"),
    SignalSpec("fineract", "http_server_error_rate", "application", "requests_per_second",
               f"sum(rate({_FINERACT_HTTP_5XX}[1m])) or (sum(rate({_FINERACT_HTTP}[1m])) * 0)",
               "prometheus:job=fineract"),
    SignalSpec("fineract", "http_latency_mean_seconds", "application", "seconds",
               f"sum(rate({_FINERACT_HTTP_SUM}[1m])) / sum(rate({_FINERACT_HTTP}[1m]))",
               "prometheus:job=fineract"),
    SignalSpec("fineract", "db_pool_active_connections", "application", "count",
               'sum(hikaricp_connections_active{job="fineract"})', "prometheus:job=fineract"),
    SignalSpec("fineract", "db_pool_pending_connections", "application", "count",
               'sum(hikaricp_connections_pending{job="fineract"})', "prometheus:job=fineract"),
    SignalSpec("fineract", "jvm_heap_used_bytes", "application", "bytes",
               'sum(jvm_memory_used_bytes{job="fineract",area="heap"})',
               "prometheus:job=fineract"),
    SignalSpec("fineract", "container_cpu_cores", "infrastructure", "cores",
               _container_cpu("aort-fineract"), "prometheus:job=cadvisor"),
    SignalSpec("fineract", "container_memory_bytes", "infrastructure", "bytes",
               _container_memory("aort-fineract"), "prometheus:job=cadvisor"),

    # ---- postgres: the banking ledger database ---------------------------------
    SignalSpec("postgres", "up", "database", "boolean",
               "max(pg_up)", "prometheus:job=postgres", bounds=UNIT_INTERVAL),
    SignalSpec("postgres", "active_connections", "database", "count",
               f"sum(pg_stat_database_numbackends{{{_PG_DB}}})", "prometheus:job=postgres"),
    SignalSpec("postgres", "commit_rate", "database", "transactions_per_second",
               f"sum(rate(pg_stat_database_xact_commit{{{_PG_DB}}}[1m]))",
               "prometheus:job=postgres"),
    SignalSpec("postgres", "rollback_rate", "database", "transactions_per_second",
               f"sum(rate(pg_stat_database_xact_rollback{{{_PG_DB}}}[1m]))",
               "prometheus:job=postgres"),
    SignalSpec("postgres", "cache_hit_ratio", "database", "ratio",
               f"sum(rate(pg_stat_database_blks_hit{{{_PG_DB}}}[5m])) / "
               f"(sum(rate(pg_stat_database_blks_hit{{{_PG_DB}}}[5m])) + "
               f"sum(rate(pg_stat_database_blks_read{{{_PG_DB}}}[5m])))",
               "prometheus:job=postgres", bounds=UNIT_INTERVAL),
    SignalSpec("postgres", "database_size_bytes", "database", "bytes",
               f"sum(pg_database_size_bytes{{{_PG_DB}}})", "prometheus:job=postgres"),
    SignalSpec("postgres", "container_cpu_cores", "infrastructure", "cores",
               _container_cpu("aort-postgres"), "prometheus:job=cadvisor"),
    SignalSpec("postgres", "container_memory_bytes", "infrastructure", "bytes",
               _container_memory("aort-postgres"), "prometheus:job=cadvisor"),

    # ---- banking-operations: business operations as customers experience them --
    SignalSpec("banking-operations", "operation_rate", "banking_workload",
               "operations_per_second",
               f"sum by (aort_category) (rate({_OPS}[1m]))",
               "prometheus:job=workload-otel", group_by=("aort_category",)),
    SignalSpec("banking-operations", "operation_error_ratio", "banking_workload", "ratio",
               f'(sum(rate({_OPS}{{aort_outcome="error"}}[1m])) or '
               f"(sum(rate({_OPS}[1m])) * 0)) / sum(rate({_OPS}[1m]))",
               "prometheus:job=workload-otel", bounds=UNIT_INTERVAL),
    SignalSpec("banking-operations", "operation_latency_mean_seconds", "banking_workload",
               "seconds",
               "sum(rate(aort_workload_operation_duration_seconds_sum[1m])) / "
               "sum(rate(aort_workload_operation_duration_seconds_count[1m]))",
               "prometheus:job=workload-otel"),

    # ---- postgres: ledger backup state (the RPO input) ---------------------------
    # Written by the banking stack's db-backup sidecar and exposed through
    # node-exporter's textfile collector. Before the first successful backup the
    # last_* series do not exist, so these read as `missing` rather than 0 -
    # "no backup yet" must never look like "a backup just now".
    SignalSpec("postgres", "backup_age_seconds", "database", "seconds",
               "time() - max(aort_backup_last_success_timestamp_seconds)",
               "prometheus:job=node"),
    SignalSpec("postgres", "backup_last_size_bytes", "database", "bytes",
               "max(aort_backup_last_size_bytes)", "prometheus:job=node"),
    SignalSpec("postgres", "backup_last_duration_seconds", "database", "seconds",
               "max(aort_backup_last_duration_seconds)", "prometheus:job=node"),
    SignalSpec("postgres", "backup_failures_total", "database", "count",
               "max(aort_backup_failures_total)", "prometheus:job=node"),

    # ---- host: the machine running the containers --------------------------------
    SignalSpec("host", "load1", "infrastructure", "load_average",
               "max(node_load1)", "prometheus:job=node"),
    SignalSpec("host", "memory_available_bytes", "infrastructure", "bytes",
               "max(node_memory_MemAvailable_bytes)", "prometheus:job=node"),
    # Prefer the filesystem holding Docker's data (and so the ledger DB volume).
    # Docker Desktop / WSL2 exposes it as /var/lib and has no "/" mountpoint;
    # a plain Linux or EC2 host usually has only "/". Either way: one series.
    SignalSpec("host", "filesystem_available_bytes", "infrastructure", "bytes",
               'max(node_filesystem_avail_bytes{mountpoint="/var/lib"}) or '
               'max(node_filesystem_avail_bytes{mountpoint="/"})',
               "prometheus:job=node"),
)
