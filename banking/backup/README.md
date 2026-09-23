# Ledger backups — Layer 1b

**Owner: Owner B · Layer 1 (Telemetry Ingestion) / input for Layer 5 (recovery)**

Real `pg_dump` backups of the Fineract tenant database, the backup-state
telemetry derived from them, and the restore path that proves those backups are
usable.

**Why this exists:** RPO is "how much banking activity would be lost if we
recovered right now". That is measurable only if backups genuinely exist and
their age is observable. This layer makes both true.

---

## How it works

```
postgres ──pg_dump──► aort_backups volume (fineract_default-<UTC>.dump)
                 │
                 └──► aort_metrics_textfile volume (aort_backup.prom)
                              │
                     node-exporter --collector.textfile
                              │
                          Prometheus ──► normalizer ──► I1 `backup_*` signals
```

The `db-backup` sidecar in `banking/docker-compose.yml` loops: dump, prune,
publish metrics, sleep. It writes to `<name>.part` and renames only on success,
so a partial dump can never be mistaken for a usable backup, and it writes the
metrics file via a temp file and rename, so node-exporter never reads a
half-written file.

Like the rest of the stack it has **no restart policy**: a sidecar that
silently restarted itself would mask an injected failure in Phase 5.

## Configuration (`banking/.env`)

| Variable | Default | Meaning |
|---|---|---|
| `AORT_BACKUP_INTERVAL_SECONDS` | 60 | Dump interval. **This sets the best achievable RPO** — at most this much activity can be lost. |
| `AORT_BACKUP_KEEP` | 5 | Dumps retained; older ones are pruned. |

## Metrics published

| Metric | Type | Meaning |
|---|---|---|
| `aort_backup_runs_total` | counter | Backup attempts since the sidecar started |
| `aort_backup_failures_total` | counter | Failed attempts |
| `aort_backup_last_success_timestamp_seconds` | gauge | Unix time of the last successful backup |
| `aort_backup_last_duration_seconds` | gauge | How long it took |
| `aort_backup_last_size_bytes` | gauge | Dump size |

**Before the first successful backup the `last_*` metrics are not emitted at
all.** They are absent rather than zero, so "no backup yet" can never be
misread as "a backup just happened". The normalizer reports them as `missing`.

As I1 signals on the `postgres` component: `backup_age_seconds` (the RPO
input), `backup_last_size_bytes`, `backup_last_duration_seconds`,
`backup_failures_total`.

## Usage

```bash
# from the repository root, banking stack running
python -m aort_backup status     # last backup, age (RPO), size, failures
python -m aort_backup list       # retained dumps
python -m aort_backup restore --into aort_restore_check
```

`status` exits non-zero if no backup has ever succeeded.

Restore is deliberately explicit: it drops, recreates and restores, stopping at
the first failing step so a half-restored database is never mistaken for a
successful recovery. Database names are **validated, not escaped**, because
they reach a command line.

## Tests

```bash
cd banking/backup
pip install -e .[test]
pytest              # unit
pytest --live       # plus real dump/restore against the running stack
```

The live suite waits for two real backups, then **restores the newest dump into
a scratch database** and checks that banking row counts (`m_client`,
`m_savings_account`, `m_loan`, `acc_gl_journal_entry`) do not exceed the live
tables, and that the restored ledger still balances (debits equal credits). An
untested backup is not evidence, so the restore is part of the gate.

## Limitations

- **Dump-based, not point-in-time.** No WAL archiving or PITR, so the RPO floor
  is the dump interval. Continuous archiving would be a genuine improvement and
  is not implemented.
- Only the tenant database (`fineract_default`) is backed up. The tenant
  registry (`fineract_tenants`) is not yet.
- Backups live in a Docker volume on the same host as the database. That is
  fine for a local prototype and **not** a real DR position; off-host storage
  (S3) belongs to the AWS phase.
- Restores are verified by row counts and ledger balance, not by a full
  byte-for-byte comparison.
