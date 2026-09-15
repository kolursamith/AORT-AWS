# aort-normalizer — Layer 1a: telemetry normalization

**Owner: Owner B · Layer 1 (Telemetry Ingestion) · Status: implemented, tested**

Turns the Phase 3 Prometheus telemetry into **I1 observation records** — the
proposed interface between the telemetry layer and the operational digital twin
(Layer 2, Owner A).

> **The I1 contract is PROVISIONAL and NOT frozen.** It is proposed in
> [`contracts/i1-observation.provisional-0.1.schema.json`](../../contracts/i1-observation.provisional-0.1.schema.json)
> for joint review. Do not build against it as if it were final.

---

## What it produces

One JSON object per signal per snapshot, for four banking-system components:

| component_id | What it represents | Example signals |
|---|---|---|
| `fineract` | Core-banking application | `up`, `http_request_rate`, `http_server_error_rate`, `http_latency_mean_seconds`, `db_pool_active_connections`, `jvm_heap_used_bytes` |
| `postgres` | Banking ledger database | `up`, `commit_rate`, `rollback_rate`, `cache_hit_ratio`, `active_connections`, `database_size_bytes` |
| `banking-operations` | Business operations as customers experience them | `operation_rate` (per `aort_category`: client, savings, loan, transaction, accounting), `operation_error_ratio`, `operation_latency_mean_seconds` |
| `host` | Machine running the containers | `load1`, `memory_available_bytes`, `filesystem_available_bytes` |

23 signals in total, covering all four blueprint data categories
(infrastructure, application, banking workload, database). The full list lives
in [`aort_normalizer/catalog.py`](aort_normalizer/catalog.py).

```json
{"schema_version": "i1-provisional-0.1",
 "snapshot_id": "650bad6d-99c7-4504-8125-3a2951fc8c3a",
 "observed_at": "2026-09-15T16:57:00.763Z",
 "component_id": "postgres", "signal": "commit_rate", "category": "database",
 "value": 3.86, "unit": "transactions_per_second", "quality": "ok",
 "source": "prometheus:job=postgres", "dimensions": {}}
```

### The `quality` field — no fabricated values

| quality | value | Meaning |
|---|---|---|
| `ok` | number | Finite and within the signal's plausible range |
| `out_of_bounds` | number | Finite but implausible (e.g. a ratio above 1) — **kept and flagged**, not dropped |
| `missing` | `null` | Prometheus returned no series (e.g. the component is gone, or no traffic in the window) |
| `non_finite` | `null` | NaN / Inf / unparseable (e.g. mean latency with zero requests) |
| `ambiguous` | `null` | More series than the signal definition allows |

A missing signal is **never** reported as 0. PromQL's `sum()` over no series is
empty, so a vanished component surfaces as `missing`.

All signals in one snapshot are evaluated at **one explicit Prometheus
evaluation time** and share a `snapshot_id`, so a snapshot is internally
consistent.

---

## Usage

Requires the banking and telemetry stacks to be running
(see [`telemetry/README.md`](../README.md)).

```bash
pip install -e telemetry/normalizer

# one snapshot to stdout
python -m aort_normalizer

# six snapshots, 10 s apart, appended to a file (builds a time series)
python -m aort_normalizer --out observations.jsonl --count 6 --interval 10
```

| Option | Default | Notes |
|---|---|---|
| `--prometheus-url` | `$AORT_PROMETHEUS_URL` or `http://localhost:9090` | |
| `--out` | stdout | Appends; a snapshot is written only once fully collected |
| `--count` | 1 | Must be ≥ 1 |
| `--interval` | 10 | Seconds between snapshots, ≥ 0 |
| `--timeout` | 10 | Per-query timeout, must be > 0 |

Exit codes: `0` success · `2` Prometheus unreachable or rejected a query ·
`3` output not writable.

Python API, for the twin:

```python
from aort_normalizer.prometheus import PrometheusClient
from aort_normalizer.normalize import collect_snapshot

observations = collect_snapshot(PrometheusClient("http://localhost:9090"))
records = [o.to_dict() for o in observations]
```

---

## Tests

```bash
cd telemetry/normalizer
pip install -e .[test]
pytest                 # unit tests (fake Prometheus)
pytest --live          # plus live tests against the running stacks
```

| File | Covers |
|---|---|
| `test_contract.py` | What the I1 schema accepts and rejects |
| `test_catalog.py` | The catalog stays inside the contract vocabulary |
| `test_prometheus_client.py` | Real API response shapes; every failure mode raises |
| `test_normalize.py` | Samples → observations; no invented values |
| `test_cli.py` | JSON Lines output, exit codes |
| `test_bug_hunt.py` | Hostile inputs and failure paths |
| `test_pipeline_contract.py` | Collector/Prometheus settings `rate()` depends on |
| `test_live_integration.py` | Real banking workload in, valid I1 out, cross-checked against direct queries |

`--live` tests **fail rather than skip** when the stack is down, so a green live
run is evidence the stack was genuinely exercised.

---

## Known limitations

- **Backup / replication state is not yet a signal.** It needs a real backup
  mechanism first (next layer, L1b); emitting a backup metric without real
  backups would be fabrication.
- `banking-operations` signals exist only while the workload generator runs
  with `OTEL_EXPORTER_OTLP_ENDPOINT` set. Otherwise they are `missing`, which is
  the truthful answer.
- Rates use 1-minute windows (cache hit ratio: 5 minutes). Mean latencies and
  the error ratio become `non_finite` (0/0) when there is no traffic in the
  window.
- **Short bursts under-report.** `rate()` cannot see the increase that happens
  before a counter series is first scraped. A workload that fires everything
  within a few seconds therefore reads as near-zero operations/s. Run the
  generator continuously (e.g. `--delay 1`) while observing — which is also how
  a digital twin is meant to be fed.
- `host` metrics on Docker Desktop describe the WSL2 VM, not the Windows host.
- The component vocabulary and the I1 fields are proposals pending the joint
  decision on stable component identity.
