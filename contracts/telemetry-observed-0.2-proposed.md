# Proposed revision `0.2` — observed telemetry fields

**Status: PROPOSED ADDITION. Nothing is frozen. Nothing in
`provisional-0.1` is overwritten.**

**Author: Owner B (Phase 3) · Reviewer needed: Owner A**

This is the Phase 3 answer to a question the blueprint says must be settled by
observation rather than design: *what does the running system actually emit?*

Every metric named here was **queried live from Prometheus** against a running
stack. None were copied from documentation, assumed from a diagram, or
invented. Anything that could not be observed is recorded in the *Gaps*
section rather than quietly filled in.

> **This is not a contract yet.** It is evidence plus a proposal. Promoting any
> of it to a frozen contract needs both owners to agree, per the ground rules
> in [README.md](README.md).

---

## How to read this

Metric types are Prometheus types as exposed by each source:

| Type | Meaning |
|---|---|
| `counter` | Monotonically increasing; use `rate()` for a per-second value |
| `gauge` | Point-in-time value that can go up or down |
| `histogram` | Exposes `_bucket`, `_sum` and `_count` series |

---

## Category 1 — Infrastructure

**Sources:** cAdvisor (`job="cadvisor"`, 114 metric names) and node-exporter
(`job="node"`, 149 metric names).

| Field | Type | Key labels | Notes |
|---|---|---|---|
| `container_cpu_usage_seconds_total` | counter | `name`, `id`, `image` | Per-container CPU seconds. `name` is the container name, e.g. `aort-fineract` |
| `container_memory_usage_bytes` | gauge | `name`, `id` | Per-container resident memory |
| `container_network_receive_bytes_total` | counter | `name`, `interface` | Also `_transmit_` |
| `container_cpu_system_seconds_total` | counter | `name` | System-time split |
| `container_cpu_user_seconds_total` | counter | `name` | User-time split |
| `container_fs_usage_bytes` | gauge | `device`, `id` | ⚠️ **machine-level only here** — see Gaps |
| `node_cpu_seconds_total` | counter | `cpu`, `mode` | Host CPU by mode |
| `node_memory_MemAvailable_bytes` | gauge | — | Host available memory |
| `node_filesystem_avail_bytes` | gauge | `device`, `mountpoint`, `fstype` | Host disk headroom |
| `node_network_receive_bytes_total` | counter | `device` | Host network |
| `node_disk_read_bytes_total` | counter | `device` | Host disk I/O |
| `node_load1` | gauge | — | 1-minute load average |

## Category 2 — Application

**Source:** Fineract's Spring Boot / Micrometer actuator
(`job="fineract"`, 128 metric names) at
`/fineract-provider/actuator/prometheus`.

| Field | Type | Key labels | Notes |
|---|---|---|---|
| `http_server_requests_seconds_count` | counter | `uri`, `method`, `status`, `outcome` | Request count per endpoint — the request-rate and error-rate source |
| `http_server_requests_seconds_sum` | counter | same | Total time; `sum/count` gives mean latency |
| `http_server_requests_seconds_max` | gauge | same | Recent maximum |
| `jvm_memory_used_bytes` | gauge | `area`, `id` | Heap and non-heap by pool |
| `jvm_threads_live_threads` | gauge | — | Live thread count |
| `jvm_gc_pause_seconds` | histogram | `action`, `cause` | GC pauses |
| `hikaricp_connections_active` | gauge | `pool` | **Fineract's own view of its DB pool** — bridges application and database |
| `hikaricp_connections_pending` | gauge | `pool` | Saturation signal |
| `hikaricp_connections_timeout_total` | counter | `pool` | Pool exhaustion |
| `process_cpu_usage` | gauge | — | Process CPU fraction |
| `application_ready_time_seconds` | gauge | — | Startup duration; useful as an RTO input after a restart |
| `executor_active_threads` | gauge | `name` | Async executors |
| `cache_gets_total` | counter | `name`, `result` | Cache hit/miss |

> `hikaricp_*` and `application_ready_time_seconds` look particularly relevant
> to the twin: the first is the application's own view of its database
> dependency, and the second is a directly measured recovery time.

## Category 3 — Banking workload (business level)

**Source:** the Phase 2 workload generator, instrumented with OpenTelemetry and
exported via the collector (`job="workload-otel"`).

Derived entirely from the generator's real per-call results — the same
`OperationResult` records behind `provisional-0.1`. No value is synthesised.

| Field | Type | Key labels | Notes |
|---|---|---|---|
| `aort_workload_operations_total` | counter | `aort_operation`, `aort_category`, `aort_outcome`, `http_request_method`, `http_route`, `http_response_status_code` | One increment per Fineract call |
| `aort_workload_operation_duration_seconds` | histogram | `aort_operation`, `aort_category`, `aort_outcome` | Client-observed latency |
| `aort_workload_http_responses_total` | counter | `aort_operation`, `http_response_status_code` | Status-code distribution |
| `traces_span_metrics_calls_total` | counter | `span_name`, `status_code`, `aort_operation`, `aort_category` | RED metrics derived from spans by the collector |
| `traces_span_metrics_duration_milliseconds` | histogram | same | Span-derived latency |

**Label vocabulary** (these are the business-level dimensions, and the part
most likely to matter to the twin):

- `aort_category` — one of `client`, `savings`, `loan`, `transaction`,
  `accounting`, `bootstrap`
- `aort_operation` — e.g. `savings.deposit`, `loan.disburse`,
  `loan.repayment`, `client.create`, `accounting.journalentry.create`
- `aort_outcome` — `ok` or `error`

Traces are also emitted (one span per Fineract call, carrying the real start
and end timestamps). They are currently only counted and logged — no tracing
backend is deployed, see Gaps.

## Category 4 — Database

**Source:** postgres_exporter (`job="postgres"`, 389 metric names).

| Field | Type | Key labels | Notes |
|---|---|---|---|
| `pg_up` | gauge | — | Exporter's view of database reachability |
| `pg_stat_database_numbackends` | gauge | `datname` | Active connections |
| `pg_stat_database_xact_commit` | counter | `datname` | Committed transactions |
| `pg_stat_database_xact_rollback` | counter | `datname` | Rollbacks — error signal |
| `pg_stat_database_blks_hit` | counter | `datname` | Buffer cache hits |
| `pg_stat_database_blks_read` | counter | `datname` | Disk reads; with `blks_hit` gives cache hit ratio |
| `pg_stat_database_tup_fetched` | counter | `datname` | Rows fetched |
| `pg_stat_database_tup_inserted` | counter | `datname` | Rows inserted |
| `pg_stat_database_deadlocks` | counter | `datname` | Contention |
| `pg_stat_database_conflicts` | counter | `datname` | Conflicts |
| `pg_database_size_bytes` | gauge | `datname` | Storage growth |
| `pg_stat_activity_count` | gauge | `datname`, `state` | Sessions by state |
| `pg_locks_count` | gauge | `datname`, `mode` | Lock pressure |

The tenant database is `datname="fineract_default"`.

---

## Gaps versus the blueprint's candidate table

Recorded rather than worked around, per the project's data-honesty rule.

| Blueprint item | Status | Detail |
|---|---|---|
| Infrastructure → disk, per container | ⚠️ **Partial** | On Docker Desktop / WSL2, cAdvisor emits `container_fs_usage_bytes` only with `id="/"` and **no `name` label** — 97 series, 0 attributable to a container. Per-container disk usage is not observable in this environment. Host-level disk via `node_filesystem_avail_bytes` works. Likely resolves on a Linux host or EC2 in Phase 10 |
| Infrastructure → host metrics | ⚠️ **Caveat** | node-exporter reports the **WSL2 Linux VM**, not the Windows host. Valid for container-level reasoning, not a true bare-metal host view |
| Application → traces | ⚠️ **Partial** | Spans are emitted and received (confirmed in collector logs) and converted to RED metrics, but **no tracing backend is deployed**, so individual traces cannot be inspected or queried. Tempo/Jaeger deliberately out of Phase 3 scope |
| Application → logs | ❌ **Not collected** | The blueprint lists logs as a telemetry input. No log pipeline exists — no Loki, no OTel log receiver wired. Phase 3 covers metrics and traces only |
| Failure labels | ❌ **Not applicable yet** | `scenario_id`, `failure_type`, `affected_component`, `severity`, `start/end` cannot exist until Phase 5 injects controlled failures |
| Recovery metrics | ❌ **Not applicable yet** | Predicted/actual RTO and RPO belong to Phases 8–11. `application_ready_time_seconds` is a plausible *measured* input for actual RTO |

---

## Open questions for Owner A

1. **Normalization boundary.** These are four vocabularies (cAdvisor,
   Micrometer, OTel, postgres_exporter) with different naming and units.
   Which subset should layer 5 normalize into a single record, and what should
   the normalized field names be?
2. **Push or pull for the twin?** The twin could query Prometheus, or consume
   a normalized stream. That choice changes what belongs in the contract.
3. **Resolution and retention.** Scrape interval is currently 10s and
   retention 7d. Is 10s the right granularity for twin state and for the
   Phase 6 dataset?
4. **Component identity.** cAdvisor uses `name="aort-fineract"`, Prometheus
   uses `job`/`instance`, the generator uses `aort_category`. The twin needs
   one stable component identifier — what should it be?
5. **Are `hikaricp_*` the right dependency signal** for the
   `fineract -> postgres` edge in the dependency graph, or do you want
   something else?

Nothing here should be treated as settled until these are answered.
