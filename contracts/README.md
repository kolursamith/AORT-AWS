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

### 3. I1 — normalized telemetry observation — 🆕 proposed by Layer 1a

**Status: PROVISIONAL (`i1-provisional-0.1`) — not agreed, not frozen.**

Schema: **[i1-observation.provisional-0.1.schema.json](i1-observation.provisional-0.1.schema.json)**
(JSON Schema draft 2020-12). Producer: `telemetry/normalizer` (Owner B).
Intended consumer: the operational digital twin, Layer 2 (Owner A).

One record per signal per snapshot: `component_id`, `signal`, `category`,
`value`, `unit`, a `quality` field, `dimensions`, and a `snapshot_id` shared by
every record taken at the same Prometheus evaluation instant.

The `quality` field is the part most worth reviewing. `missing`, `non_finite`
and `ambiguous` observations carry `value: null`, and the schema *rejects* a
number in those cases, so an absent signal can never be passed off as a zero.

**What Owner A needs to decide:**

- [ ] Is the proposed `component_id` vocabulary (`fineract`, `postgres`,
      `banking-operations`, `host`) the stable component identity the twin
      will use?
- [ ] Is snapshot-per-poll (pull) the right shape, or does the twin want a
      stream of individual observations?
- [ ] Only then: freeze as `i1-1.0`.

Backup/replication state has since been added (Layer 1b) as four signals on the
existing `postgres` component — `backup_age_seconds` (the RPO input),
`backup_last_size_bytes`, `backup_last_duration_seconds`,
`backup_failures_total`. That needed **no change to this schema**: no new
component, no version bump.

### 4. I4 — controlled failure injection event — 🆕 proposed by Phase 5

**Status: PROVISIONAL (`i4-injection-provisional-0.1`) — not agreed, not frozen.**

Schema: **[i4-injection-event.provisional-0.1.schema.json](i4-injection-event.provisional-0.1.schema.json)**.
Producer: `infrastructure/injection` (Owner B). Consumers: the experiment
dataset (Phase 6) and supervised learning (Phase 7).

One record per phase of an injection (`start`, `end`, or `failed`), sharing an
`injection_id`: which `scenario`, which `component_id`, which container, the
parameters, and the exact UTC time. These are the **labels** — a metric moving
is evidence only once the moment a failure began and ended is recorded
independently of the telemetry it perturbs.

**What Owner A needs to decide** (scenario semantics are yours):

- [ ] Are `service_stop`, `service_pause`, `cpu_throttle` and `backup_stop` the
      right scenario vocabulary, and what should compound scenarios look like?
- [ ] Does the twin need a separate *scenario descriptor* (I3) that this event
      references, or is the event itself enough?
- [ ] `component_id` here is `fineract | postgres | backup`, while I1 reports
      backup state as signals on `postgres`. Which identity wins? This is the
      same open question as the stable component identifier above.
- [ ] Should severity be an explicit field rather than implied by parameters?

### 5. Twin state and scenario descriptors — ⏳ not started

Owner A's Phase 4/5 work. Listed so the dependency is visible.
