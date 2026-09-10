# contracts/ — Shared Interfaces

**Owners: both (Owner A and Owner B jointly) · Phase 1**

This directory holds the interfaces the modules agree on: telemetry record
shapes, twin state structures, scenario descriptors, and anything else one
owner produces and another consumes.

## Status: nothing frozen yet

**No contract has been agreed or frozen.** Phase 1 has not been completed.

This directory exists to make that gap visible rather than implicit, and to
give the pending proposals a reviewable home.

> **Note on history:** this directory was first proposed in PR #2, which was
> closed unmerged when repository governance moved to
> `feature/repository-governance`. `.github/CODEOWNERS` on `main` already
> routes `/contracts/` to both owners, but the directory itself was never
> created. It is reintroduced here with the Phase 2 record carried forward
> unchanged plus the Phase 3 observations added.

## Ground rules

- Contracts are **jointly owned**. `.github/CODEOWNERS` requires both owners to
  review changes here.
- **Neither owner may freeze a contract unilaterally.** A schema marked
  provisional stays provisional until both owners agree in a PR.
- Per the blueprint: **freeze the input contract before freezing the exact
  dataset schema**, and only after the real telemetry surface has been
  inspected on the running system.
- A field appearing in a design document or architecture diagram is **not**
  evidence that anything emits it.

---

## Pending items

### 1. Banking workload run-record schema — ⏳ pending joint agreement

**Status: PROVISIONAL (`provisional-0.1`) — not agreed, not frozen.**

Phase 2 (`banking/`) produces per-call JSONL run records via
`banking/workload/aort_workload/report.py`.

Each record describes one Fineract API call as observed: run id, sequence
number, timestamp, category, operation, HTTP method and path, status code,
success flag, latency in milliseconds, returned resource id, and any error
message returned.

Unchanged by Phase 3. Still awaiting Owner A's review.

### 2. Observed telemetry fields — 🆕 proposed revision from Phase 3

**Status: PROPOSED ADDITION — for review, nothing overwritten.**

See **[telemetry-observed-0.2-proposed.md](telemetry-observed-0.2-proposed.md)**.

Phase 3 validated what the running stack actually emits. That document lists
the real metric names, types and sample values observed live, as a proposed
`0.2` revision on top of `provisional-0.1`.

It is a **proposal, not a decision**. `provisional-0.1` remains the current
provisional record until both owners agree otherwise.

**What Owner A needs to decide:**

- [ ] Are these the right fields for twin ingestion (Phase 4)?
- [ ] Which belong in the normalized telemetry contract versus staying
      source-specific implementation detail?
- [ ] What normalization does layer 5 need — units, naming, label
      reconciliation across the four sources?
- [ ] Only then: promote an agreed subset to a versioned contract here.

### 3. Twin state and scenario descriptors — ⏳ not started

Owner A's Phase 4/5 work. Listed so the dependency is visible.
