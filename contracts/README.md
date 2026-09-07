# contracts/ — Shared Interfaces

**Owners: both (Owner A and Owner B jointly) · Phase 1**

This directory holds the interfaces the modules agree on: telemetry record
shapes, twin state structures, scenario descriptors, and anything else one
owner produces and another consumes.

## Status: empty, and deliberately so

**No contract has been agreed or frozen yet.** Phase 1 has not been completed.

This directory currently exists to make that gap visible rather than implicit,
and to give the pending items a home.

## Ground rules

- Contracts are **jointly owned**. `.github/CODEOWNERS` requires both owners to
  review changes here.
- **Neither owner may freeze a contract unilaterally.** A schema marked
  provisional stays provisional until both owners agree in a PR.
- Per the blueprint: **freeze the input contract before freezing the exact
  dataset schema**, and only after the real telemetry surface has been
  inspected on the running system. Do not specify fields that the system has
  not been observed to emit.
- A field appearing in a design document or architecture diagram is **not**
  evidence that anything emits it.

---

## Pending items

### 1. Banking workload run-record schema — ⏳ pending joint agreement

**Status: PROVISIONAL — not agreed, not frozen.**

Phase 2 (`banking/`) produces per-call JSONL run records via
`banking/workload/aort_workload/report.py`, currently stamped
`"schema_version": "provisional-0.1"`.

Each record describes one Fineract API call as actually observed: run id,
sequence number, timestamp, category, operation, HTTP method and path, status
code, success flag, latency in milliseconds, returned resource id, and any
error message returned.

This is a **likely** input to the digital twin (Phase 4) and the experiment
dataset (Phase 6), which is exactly why it must not be settled by Owner B
alone.

**What needs to happen:**

- [ ] Owner A reviews whether these fields are the right shape for twin
      ingestion and dataset construction
- [ ] Both owners agree what belongs in the contract versus what stays a
      Phase 2 implementation detail
- [ ] Only then: promote to a versioned contract in this directory

Until that happens, treat the current output as an implementation detail of
`banking/` that may change. It carries no compatibility guarantee.

### 2. Telemetry contract — ⏳ not started

Phase 3 must inspect what the running system actually emits (Fineract's
Prometheus actuator endpoint, container metrics, PostgreSQL metrics) before any
normalized telemetry contract is written here.

### 3. Twin state and scenario descriptors — ⏳ not started

Owner A's Phase 4/5 work. Listed here so the dependency is visible.
