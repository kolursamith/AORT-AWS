"""Ledger-database backup state and restore.

The banking stack runs a sidecar that takes real `pg_dump` backups of the
Fineract tenant database on a loop and writes backup-state metrics for
node-exporter to expose. This package reads that state and performs restores.

Backup age is the direct input for RPO: it is how much banking activity would
be lost if the database were recovered from the newest backup right now.
"""

__version__ = "0.1.0"
