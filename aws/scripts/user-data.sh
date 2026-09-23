#!/bin/bash
# AORT prototype bootstrap, run once by EC2 user data.
#
# Starts the same banking and telemetry stacks used locally - nothing about
# them is AWS-specific - then installs the backup shipper to S3.
#
# Expects: AORT_BACKUP_BUCKET, AORT_LOG_GROUP, AORT_REGION
set -euo pipefail

REPO_DIR="${AORT_REPO_DIR:-/opt/aort}"
: "${AORT_BACKUP_BUCKET:?AORT_BACKUP_BUCKET is required}"
: "${AORT_REGION:?AORT_REGION is required}"

echo "aort: bootstrapping in ${REPO_DIR}"
cd "${REPO_DIR}"

# Configuration comes from the committed templates; no secrets are baked into
# the AMI or the template. Change these before any non-prototype use.
[ -f banking/.env ] || cp banking/.env.example banking/.env
[ -f telemetry/.env ] || cp telemetry/.env.example telemetry/.env

echo "aort: starting the banking stack"
docker compose -f banking/docker-compose.yml up -d

echo "aort: waiting for Fineract to finish its Liquibase migrations"
for _ in $(seq 1 120); do
    if curl -fsS http://localhost:8080/fineract-provider/actuator/health 2>/dev/null \
        | grep -q '"UP"'; then
        echo "aort: fineract is up"
        break
    fi
    sleep 5
done

echo "aort: starting the telemetry stack"
docker compose -f telemetry/docker-compose.yml up -d

# Ship ledger backups off the instance. Backups that live only on the host
# they protect are not a disaster-recovery position.
install -m 0755 aws/scripts/backup-to-s3.sh /usr/local/bin/aort-backup-to-s3
cat >/etc/cron.d/aort-backup-to-s3 <<CRON
AORT_BACKUP_BUCKET=${AORT_BACKUP_BUCKET}
AORT_REGION=${AORT_REGION}
AORT_REPO_DIR=${REPO_DIR}
*/5 * * * * root /usr/local/bin/aort-backup-to-s3 >> /var/log/aort-backup-to-s3.log 2>&1
CRON

echo "aort: bootstrap complete"
