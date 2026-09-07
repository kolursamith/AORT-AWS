#!/bin/bash
# Provisions the two databases Apache Fineract requires before first boot:
#   * tenant registry  - maps tenant identifiers to their data stores
#   * default tenant   - holds the actual banking data
# Fineract runs its own Liquibase migrations against these on startup; this
# script only creates the empty databases and the role that owns them.
#
# Runs once, on an empty data volume. Re-running requires: docker compose down -v
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE USER ${FINERACT_DB_USER} WITH PASSWORD '${FINERACT_DB_PASSWORD}';
    CREATE DATABASE ${FINERACT_TENANTS_DB} OWNER ${FINERACT_DB_USER};
    CREATE DATABASE ${FINERACT_TENANT_DEFAULT_DB} OWNER ${FINERACT_DB_USER};
EOSQL

# PostgreSQL 15+ no longer grants CREATE on the public schema by default, so the
# owning role is granted explicitly in each database.
for db in "${FINERACT_TENANTS_DB}" "${FINERACT_TENANT_DEFAULT_DB}"; do
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$db" <<-EOSQL
        GRANT ALL ON SCHEMA public TO ${FINERACT_DB_USER};
        ALTER SCHEMA public OWNER TO ${FINERACT_DB_USER};
EOSQL
done

echo "AORT: Fineract databases provisioned (${FINERACT_TENANTS_DB}, ${FINERACT_TENANT_DEFAULT_DB})"
