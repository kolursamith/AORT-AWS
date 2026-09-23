# Controlled failure injection

**Owner: Owner B · Phase 5 (failure-injection infrastructure)**

Applies a reversible, allow-listed failure to one banking component, holds it,
restores it, and records exactly what was done and when.

Those records are the **ground truth** that makes telemetry interpretable: a
metric moving is evidence only once you know a specific failure began at a
known moment on a known component. They are the label source for the Phase 6
dataset and Phase 7 supervised learning.

> **Location note.** This lives in `infrastructure/` because `.github/CODEOWNERS`
> assigns `/infrastructure/` to Owner B while `/scenarios/` belongs to Owner A.
> The blueprint splits scenarios into *semantics* (Owner A) and *injection
> infrastructure* (Owner B); that split is an open joint decision. If it is
> settled, this moves to `scenarios/injection/` unchanged.

---

## Scenarios

| Scenario | What it does | Banking meaning |
|---|---|---|
| `service_stop` | stops the container | the service is gone; requests fail outright |
| `service_pause` | freezes the container | hung service: connections stall, **no data is lost** |
| `cpu_throttle` | caps the CPU quota | resource exhaustion; still serving, slowly |
| `backup_stop` | stops the backup sidecar | the bank keeps trading while **RPO silently degrades** |

Targets are an allow-list: `fineract` (core banking), `postgres` (ledger
database), `backup` (backup sidecar). Anything else is refused — injection that
could stop Prometheus or the collector would destroy the experiment's own
observation.

**Nothing destroys data or containers.** No `rm`, `kill`, `volume` or `down`; a
unit test asserts those verbs can never appear in a built command.

## Usage

```bash
python -m aort_injection list
python -m aort_injection inject --scenario service_pause --component fineract --duration 30
python -m aort_injection inject --scenario cpu_throttle --component fineract --cpus 0.2
python -m aort_injection inject --scenario backup_stop --component backup --duration 120
```

Events are appended to `infrastructure/injection/runs/injections.jsonl`
(git-ignored) as I4 records — see
[`contracts/i4-injection-event.provisional-0.1.schema.json`](../../contracts/i4-injection-event.provisional-0.1.schema.json),
which is **provisional and not frozen**.

Exit codes: `0` injected and reverted · `2` invalid parameter · `3` injection
failed · `130` interrupted (the injection was still reverted).

## Safety properties, each covered by a test

- **Always reverted** — including when the hold is interrupted (Ctrl-C) *and*
  when writing the event file fails. Restoring the system outranks bookkeeping.
- **Refuses a target that is not already running**, so labels never claim this
  experiment caused a failure that was already there.
- **Verifies recovery**: after reverting it re-inspects the container and fails
  loudly if it is not running again.
- **A failed revert is loud**, and recorded, because the stack is left perturbed.
- **Validates before touching anything**: an invalid duration, component or
  scenario issues no docker command at all.

## Tests

```bash
cd infrastructure/injection
pip install -e .[test]
pytest              # unit + bug hunt
pytest --live       # real injections into the running stack
```

The live suite stops the backup sidecar, pauses Fineract, and asserts the
outage is **visible in Prometheus** (`min_over_time(up{job="fineract"}[3m])`
reaches 0) before recovering — then checks the whole stack is healthy again.

## Limitations

- **No network fault injection.** Latency and packet loss need `tc`/NET_ADMIN
  inside the target's network namespace, which the Fineract and Postgres images
  do not provide. Doing it properly needs a privileged helper container; it is
  not implemented, so database/network *latency* scenarios from the blueprint
  are **not** available yet.
- No compound scenarios (two failures at once).
- `cpu_throttle` changes the CPU quota; it does not generate load.
- Docker-level only. AWS-side fault injection (FIS) is out of scope and costs
  money.
