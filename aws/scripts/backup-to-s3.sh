#!/bin/bash
# Copy ledger dumps off the instance into S3.
#
# The local sidecar keeps backups in a Docker volume on the same host as the
# database. That protects against a dropped table, not against losing the
# host, so the dumps are shipped to S3 where the RPO story actually holds.
#
# Expects: AORT_BACKUP_BUCKET, AORT_REGION, AORT_REPO_DIR
set -euo pipefail

REPO_DIR="${AORT_REPO_DIR:-/opt/aort}"
: "${AORT_BACKUP_BUCKET:?AORT_BACKUP_BUCKET is required}"
: "${AORT_REGION:?AORT_REGION is required}"

STAGING="$(mktemp -d)"
trap 'rm -rf "${STAGING}"' EXIT

# Pull the dumps out of the sidecar rather than reaching into Docker's
# internal volume paths, which are not a stable interface.
docker compose -f "${REPO_DIR}/banking/docker-compose.yml" \
    cp db-backup:/backups "${STAGING}/backups"

if [ ! -d "${STAGING}/backups" ]; then
    echo "aort-backup-to-s3: no backups directory to ship" >&2
    exit 1
fi

aws s3 sync "${STAGING}/backups" "s3://${AORT_BACKUP_BUCKET}/backups/" \
    --region "${AORT_REGION}" \
    --only-show-errors

echo "aort-backup-to-s3: shipped $(find "${STAGING}/backups" -name '*.dump' | wc -l) dump(s)"
