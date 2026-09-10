# AORT — Telemetry Validation (Phase 3)

Real, observable telemetry from every layer of the running system, and a
validation that proves each of the blueprint's four data categories is
producing genuine, changing values.

This phase is about **observation and evidence**. It deliberately does *not*
build the normalization layer (layer 5) or the digital twin (layer 6).

> **Grafana is included here for human visual verification only. It is not the
> digital twin.** The twin is a separate custom model in `digital-twin/`,
> owned by Owner A and built in Phase 4. Prometheus, Grafana and cAdvisor are
> observability components; the twin is the project's own representation of
> component state, dependencies and recovery context.

---

## Contents

| Path | Purpose |
|---|---|
| `docker-compose.yml` | Telemetry stack, composed alongside `banking/` |
| `.env.example` | Configuration template (copy to `.env`) |
| `prometheus/prometheus.yml` | Scrape configuration for all six targets |
| `otel-collector/config.yml` | OTLP receiver, spanmetrics, Prometheus exposition |
| `instrumentation/` | `aort_telemetry` — OTel instrumentation for the Phase 2 generator |
| `grafana/` | Provisioned datasource and overview dashboard |
| `validate/validate_telemetry.py` | The Phase 3 evidence script |
| `evidence/` | Captured output from the clean-slate validation run |

---

## Architecture

```
                        banking/ (Phase 2)
        ┌───────────────────────────────────────────┐
        │  Fineract  ──────────────►  PostgreSQL    │
        └───────┬───────────────────────────┬───────┘
                │ actuator/prometheus        │
                │                            │ pg_stat_*
                ▼                            ▼
          [2] Application            [4] postgres_exporter
                │                            │
   workload generator ──OTLP──► otel-collector          cAdvisor + node-exporter
      [3] Banking workload          │  spanmetrics            [1] Infrastructure
                                    │                            │
                                    ▼                            ▼
                            ┌──────────────────────────────────────┐
                            │             Prometheus               │
                            └──────────────────┬───────────────────┘
                                               ▼
                                            Grafana
                                   (visual verification only)
```

The telemetry stack **joins the network the banking stack already creates**
(`aort-banking_default`, declared `external: true`). It extends Phase 2 rather
than replacing or duplicating it, so `banking/` still runs standalone.

---

## Starting the full stack

The banking stack must be running first — the telemetry stack attaches to its
network.

```bash
# 1. Banking foundation (Phase 2)
cd banking
cp .env.example .env
docker compose up -d          # wait ~60-90s for Fineract's Liquibase migrations

# 2. Telemetry (Phase 3)
cd ../telemetry
cp .env.example .env
docker compose up -d
```

| Service | URL |
|---|---|
| Prometheus | http://localhost:9090 |
| Grafana | http://localhost:3000 (anonymous viewer, or `admin`/`admin`) |
| cAdvisor | http://localhost:8081 |
| node-exporter | http://localhost:9100/metrics |
| postgres_exporter | http://localhost:9187/metrics |
| OTel collector (Prometheus exposition) | http://localhost:8889/metrics |
| OTLP ingest | `http://localhost:4318` (HTTP), `localhost:4317` (gRPC) |

Confirm all six scrape targets are healthy:

```bash
curl -s http://localhost:9090/api/v1/targets \
  | python -c "import sys,json;[print(t['labels']['job'],t['health']) for t in json.load(sys.stdin)['data']['activeTargets']]"
```

### Teardown

```bash
cd telemetry && docker compose down -v   # -v also drops Prometheus/Grafana data
cd ../banking && docker compose down -v
```

Tear down telemetry **before** banking, since telemetry depends on banking's
network.

---

## Instrumenting the workload generator

The Phase 2 generator is reused in place — not forked. Instrumentation lives
in `telemetry/instrumentation` and `banking/` only gained a lazy optional
import, so Phase 2 still runs standalone and its 19-check verification is
unaffected.

```bash
pip install -r banking/workload/requirements.txt
pip install -e telemetry/instrumentation

# Setting the endpoint is what activates instrumentation.
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
python -m aort_workload --clients 5 --iterations 60 --seed 42
```

Without `OTEL_EXPORTER_OTLP_ENDPOINT`, `aort_telemetry` is a **no-op** and the
generator behaves exactly as it did in Phase 2.

It emits, per Fineract call the generator actually made:

- a **span**, reconstructed with the call's real start and end timestamps
- `aort_workload_operations_total` — counter by operation, category, outcome
- `aort_workload_operation_duration_seconds` — latency histogram
- `aort_workload_http_responses_total` — status-code counter

All of it derives from the same `OperationResult` records Phase 2 already
produced from real HTTP responses. **No metric value is synthesised.**

---

## What is actually observable

Every metric below was **queried live from Prometheus** on a running stack.
Nothing is copied from documentation or assumed from a diagram. Full field
tables, with types and labels, are in
[`contracts/telemetry-observed-0.2-proposed.md`](../contracts/telemetry-observed-0.2-proposed.md).

Metric-name counts per source, as reported by Prometheus:

| Source | Job | Distinct metric names |
|---|---|---|
| Fineract actuator | `fineract` | **128** |
| postgres_exporter | `postgres` | **389** |
| cAdvisor | `cadvisor` | **114** |
| node-exporter | `node` | **149** |

### 1. Infrastructure — cAdvisor + node-exporter

`container_cpu_usage_seconds_total`, `container_cpu_system_seconds_total`,
`container_cpu_user_seconds_total`, `container_memory_usage_bytes`,
`container_network_receive_bytes_total` / `_transmit_`,
`container_fs_usage_bytes`, `container_fs_limit_bytes`,
`container_blkio_device_usage_total`, `node_cpu_seconds_total`,
`node_memory_MemAvailable_bytes`, `node_filesystem_avail_bytes`,
`node_network_receive_bytes_total`, `node_disk_read_bytes_total`,
`node_load1`

Per-container series carry a `name` label such as `name="aort-fineract"`.

### 2. Application — Fineract actuator

`http_server_requests_seconds_count` / `_sum` / `_max` (labelled by `uri`,
`method`, `status`, `outcome`), `jvm_memory_used_bytes`,
`jvm_threads_live_threads`, `jvm_gc_pause_seconds`,
`hikaricp_connections_active` / `_pending` / `_timeout_total`,
`process_cpu_usage`, `application_ready_time_seconds`,
`executor_active_threads`, `cache_gets_total`

Two worth flagging to Owner A: `hikaricp_*` is Fineract's own view of its
PostgreSQL dependency, and `application_ready_time_seconds` is a directly
**measured** startup time — a plausible real input for actual RTO later.

### 3. Banking workload — OpenTelemetry from the generator

`aort_workload_operations_total`, `aort_workload_operation_duration_seconds`
(`_bucket`/`_sum`/`_count`), `aort_workload_http_responses_total`,
`traces_span_metrics_calls_total`,
`traces_span_metrics_duration_milliseconds`

Business-level labels: `aort_category` (`client`, `savings`, `loan`,
`transaction`, `accounting`, `bootstrap`), `aort_operation`
(e.g. `savings.deposit`, `loan.disburse`), `aort_outcome` (`ok`/`error`).

### 4. Database — postgres_exporter

`pg_up`, `pg_stat_database_numbackends`, `pg_stat_database_xact_commit` /
`_xact_rollback`, `pg_stat_database_blks_hit` / `_blks_read`,
`pg_stat_database_tup_fetched` / `_tup_inserted`,
`pg_stat_database_deadlocks`, `pg_stat_database_conflicts`,
`pg_database_size_bytes`, `pg_stat_activity_count`, `pg_locks_count`

Tenant database: `datname="fineract_default"`.

---

## Validation

```bash
# with both stacks running
python telemetry/validate/validate_telemetry.py
```

It confirms all six scrape targets are up, samples every category, drives real
banking activity through the Phase 2 generator, waits for the scrape and
export intervals, re-samples, and requires that values are **present and
actually moving**. Exits non-zero on failure.

A series that did not exist at baseline and carries a value afterwards is
counted as changed — on a genuinely clean slate the workload metrics *cannot*
exist before the workload has run, so appearing is the strongest evidence
available, not the weakest.

### Verified result

Run from a genuine clean slate on 2026-09-09 — **both** stacks torn down with
`docker compose down -v` (destroying the PostgreSQL, Prometheus and Grafana
volumes), then rebuilt, then validated.

```
Scrape targets
  [PASS] Prometheus reachable - 6 active targets
  [PASS] Target up: cadvisor / fineract / node / postgres / prometheus / workload-otel

Baseline sample
  1. Infrastructure: 8/8 metrics present
  2. Application: 4/6 metrics present
  3. Banking workload: 0/4 metrics present      <- nothing had run yet
  4. Database: 6/6 metrics present

  [PASS] Workload generated real activity - 127/127 Fineract calls succeeded

1. Infrastructure
    container_cpu_usage_seconds_total        136.930841 -> 164.437632   (changed)
    container_memory_usage_bytes             1804582912 -> 2018488320   (changed)
    container_network_receive_bytes_total      65556280 -> 90642460     (changed)
    node_cpu_seconds_total                     90061.93 -> 90460.32     (changed)
    node_memory_MemAvailable_bytes           5301321728 -> 5052858368   (changed)
  [PASS] 8/8 present, 8 appeared or moved

2. Application
    http_server_requests_seconds_count             None -> 134.0        (appeared)
    http_server_requests_seconds_sum               None -> 11.2490755   (appeared)
    jvm_memory_used_bytes                     694305896 -> 692329552    (changed)
    process_cpu_usage                               0.0 -> 0.00138104   (changed)
  [PASS] 6/6 present, 5 appeared or moved

3. Banking workload
    aort_workload_operations_total                 None -> 127.0        (appeared)
    aort_workload_operation_duration_seconds_count None -> 127.0        (appeared)
    aort_workload_operation_duration_seconds_sum   None -> 11.67444     (appeared)
    aort_workload_http_responses_total             None -> 127.0        (appeared)
  [PASS] 4/4 present, 4 appeared or moved

4. Database
    pg_stat_database_xact_commit                   4617 -> 7201         (changed)
    pg_stat_database_blks_hit                    462117 -> 674859       (changed)
    pg_stat_database_tup_fetched                 405063 -> 514364       (changed)
    pg_database_size_bytes                     23417315 -> 25113059     (changed)
  [PASS] 6/6 present, 4 appeared or moved

========================================================================
TELEMETRY VALIDATION PASSED - all 16 checks passed
========================================================================
```

The `0/4` baseline for category 3 is the point: on a clean slate the workload
metrics genuinely did not exist, so reaching 127 is evidence they were produced
by this run and not left over from an earlier one.

### Business-level detail, straight from Prometheus

The banking-workload category is not just a request count — it carries the
business dimensions the blueprint asks for:

```
sum by (aort_category) (aort_workload_operations_total)
    accounting=7   bootstrap=27   client=14   loan=18   savings=23   transaction=38

topk(10, sum by (aort_operation) (aort_workload_operations_total))
    savings.deposit=23   savings.read=11   loan.repayment=9   client.list=7
    savings.withdrawal=6   loan.approve=6   loan.disburse=6   loan.apply=6

sum by (aort_outcome) (aort_workload_operations_total)
    ok=127
```

Span-derived RED metrics agree exactly with the counters
(`savings.deposit=23`, `loan.repayment=9`, …), confirming both the trace and
metric pipelines carry the same real activity.

### Phase 2 was not broken

With the instrumentation present but **no** `OTEL_EXPORTER_OTLP_ENDPOINT` set,
the Phase 2 verification still passes:

```
  [PASS] Every Fineract call succeeded - 65/65 succeeded
  VERIFICATION PASSED - all 19 checks passed
```

### Captured evidence

| File | Contents |
|---|---|
| `evidence/clean-slate-validation.txt` | Full console output of the clean-slate run |
| `evidence/telemetry-validation-*.json` | Machine-readable before/after values per metric |
| `evidence/prometheus-raw-queries.txt` | Unedited Prometheus responses, independent of the validation script |


---

## Known gaps versus the blueprint's candidate table

Recorded rather than worked around, per the project's data-honesty rule.

| Blueprint item | Status | Detail |
|---|---|---|
| Infrastructure → disk, **per container** | ⚠️ Partial | On Docker Desktop / WSL2, cAdvisor emits `container_fs_usage_bytes` only with `id="/"` and **no `name` label** — 97 series, none attributable to a container. Per-container disk usage is not observable in this environment. Host-level disk works. Expected to resolve on a Linux host or EC2 in Phase 10 |
| Infrastructure → host metrics | ⚠️ Caveat | node-exporter reports the **WSL2 Linux VM**, not the Windows host. Valid for container-level reasoning; not a bare-metal host view |
| Application → traces | ⚠️ Partial | Spans are emitted, received and converted to RED metrics, but **no tracing backend is deployed**, so individual traces cannot be queried or inspected. Tempo/Jaeger deliberately out of scope |
| Application → **logs** | ❌ Not collected | The blueprint lists logs as an input. No log pipeline exists — no Loki, no OTel log receiver. **Phase 3 covers metrics and traces only** |
| Failure labels | ❌ Not applicable yet | `scenario_id`, `failure_type`, `affected_component`, `severity`, `start`/`end` cannot exist until Phase 5 injects controlled failures |
| Recovery metrics | ❌ Not applicable yet | Predicted/actual RTO and RPO belong to Phases 8–11 |

---

## Design decisions

**Composes alongside `banking/`, does not replace it.** The telemetry stack
attaches to the banking network as `external`. `banking/` keeps working on its
own, and its Phase 2 verification is unaffected.

**Instrumentation lives in `telemetry/`, not `banking/`.** `banking/` gained
only a lazy optional import and one recording call — 12 added lines, no
control-flow change, no deletions. Without the telemetry package or the OTLP
env var, Phase 2 behaves identically.

**Spans are reconstructed with real timestamps.** `record_call` creates the
span using the call's true start and end, so no wrapper had to be threaded
through the generator's control flow.

**Telemetry never breaks the workload.** Every instrumentation path is wrapped
so that an export failure degrades to a warning instead of failing a banking
run.

**Images are pinned** — `prom/prometheus:v3.14.0`, `postgres-exporter:v0.20.1`,
`cadvisor:v0.55.1`, `node-exporter:v1.12.1`,
`opentelemetry-collector-contrib:0.160.0`, `grafana:13.2.1`.

**No normalization layer.** Converting these four vocabularies into one
contract is layer 5 and a joint contract decision — see the open questions in
`contracts/telemetry-observed-0.2-proposed.md`.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `network aort-banking_default not found` | The banking stack is not running. Start it first |
| `workload-otel` target up but no `aort_*` metrics | The generator ran without `OTEL_EXPORTER_OTLP_ENDPOINT` set, so instrumentation was inert |
| `node-exporter` fails with "not a shared or slave mount" | `rslave` propagation is unsupported on Docker Desktop; this compose already uses a plain read-only bind |
| cAdvisor returns no per-container `container_fs_*` | Known environment gap — see above |
| Prometheus target down after restart | Give it one scrape interval (10s), then check `docker compose logs` for that exporter |
| Grafana shows "No data" | Check the time range; the stack defaults to the last 30 minutes and needs a workload run to show activity |

---

## Where this fits

- **Phase 2 (`banking/`)** supplies the workload being observed.
- **Phase 4 (`digital-twin/`, Owner A)** consumes validated telemetry. The
  observed fields are proposed for review in
  `contracts/telemetry-observed-0.2-proposed.md` — **not frozen**.
- **Phase 5 (`scenarios/`)** will inject controlled failures; these same
  metrics are how their impact becomes observable. Neither banking container
  sets a Docker restart policy, so an injected failure is not silently undone.
- **Phase 10 (`aws/`)** replaces or supplements these exporters with
  CloudWatch. Fineract already exposes a `FINERACT_MANAGEMENT_CLOUDWATCH_ENABLED`
  flag, unused so far.
