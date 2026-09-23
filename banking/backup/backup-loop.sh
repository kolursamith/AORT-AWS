#!/bin/sh
# AORT ledger-database backup sidecar.
#
# Takes a real pg_dump of the Fineract tenant database on a loop, keeps the
# newest few dumps, and publishes backup state as a Prometheus textfile that
# node-exporter exposes. Backup age is the input for RPO.
#
# Deliberate choices:
#   * dumps are written to <name>.part and renamed only on success, so a
#     partial dump can never be mistaken for a usable backup
#   * the metrics file is written to a temp file and renamed, so node-exporter
#     never reads a half-written file
#   * last_* metrics are omitted entirely until a backup has actually
#     succeeded - an absent backup must not look like a fresh one
set -eu

DB="${AORT_BACKUP_DATABASE:?AORT_BACKUP_DATABASE is required}"
DIR="${AORT_BACKUP_DIR:-/backups}"
TEXTFILE="${AORT_BACKUP_TEXTFILE:-/textfile/aort_backup.prom}"
INTERVAL="${AORT_BACKUP_INTERVAL_SECONDS:-60}"
KEEP="${AORT_BACKUP_KEEP:-5}"

runs=0
failures=0
last_success=""
last_duration=""
last_size=""

mkdir -p "$DIR" "$(dirname "$TEXTFILE")"

write_metrics() {
    tmp="${TEXTFILE}.tmp"
    {
        echo "# HELP aort_backup_runs_total Ledger backup attempts since the sidecar started."
        echo "# TYPE aort_backup_runs_total counter"
        echo "aort_backup_runs_total ${runs}"
        echo "# HELP aort_backup_failures_total Failed ledger backup attempts."
        echo "# TYPE aort_backup_failures_total counter"
        echo "aort_backup_failures_total ${failures}"
        if [ -n "$last_success" ]; then
            echo "# HELP aort_backup_last_success_timestamp_seconds Unix time of the last successful backup."
            echo "# TYPE aort_backup_last_success_timestamp_seconds gauge"
            echo "aort_backup_last_success_timestamp_seconds ${last_success}"
            echo "# HELP aort_backup_last_duration_seconds Duration of the last successful backup."
            echo "# TYPE aort_backup_last_duration_seconds gauge"
            echo "aort_backup_last_duration_seconds ${last_duration}"
            echo "# HELP aort_backup_last_size_bytes Size of the last successful backup."
            echo "# TYPE aort_backup_last_size_bytes gauge"
            echo "aort_backup_last_size_bytes ${last_size}"
        fi
    } > "$tmp"
    chmod 0644 "$tmp"
    mv "$tmp" "$TEXTFILE"
}

# Publish the zero state immediately, so "no backup yet" is visible rather than
# looking like a missing exporter.
write_metrics
echo "aort-backup: backing up ${DB} every ${INTERVAL}s, keeping ${KEEP}"

while : ; do
    runs=$((runs + 1))
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    target="${DIR}/${DB}-${stamp}.dump"
    started=$(date +%s%N)

    if pg_dump --username="${PGUSER}" --dbname="${DB}" \
               --format=custom --file="${target}.part"; then
        mv "${target}.part" "$target"
        finished=$(date +%s%N)
        last_success=$(date +%s)
        last_duration=$(awk "BEGIN{printf \"%.3f\", (${finished} - ${started}) / 1000000000}")
        last_size=$(stat -c %s "$target")
        echo "aort-backup: wrote ${target} (${last_size} bytes in ${last_duration}s)"
    else
        failures=$((failures + 1))
        rm -f "${target}.part"
        echo "aort-backup: backup FAILED for ${DB}" >&2
    fi

    # Retention: names sort chronologically, so keep the last $KEEP.
    if [ "$KEEP" -gt 0 ]; then
        ls -1 "${DIR}"/*.dump 2>/dev/null | sort | head -n "-${KEEP}" | while read -r old; do
            rm -f "$old"
            echo "aort-backup: pruned ${old}"
        done
    fi

    write_metrics
    sleep "$INTERVAL"
done
