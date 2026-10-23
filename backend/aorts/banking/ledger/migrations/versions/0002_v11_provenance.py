"""v1.1 provenance: evidence records, check identity, experiment, telemetry windows, delivery

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-23

Additive only (Stage 09R integration). No existing table is dropped or rewritten, no money
column or ledger constraint changes, and no historical row is backfilled with invented data:

* ``transactions.experiment_id`` / ``transactions.check_in_flight_id``: nullable. Rows created
  before this migration keep NULL (their experiment and check identity were never recorded).
* ``evidence_records``: one row per signed switch response the remitter received, with the
  signature material exactly as received (key id, timestamp, signature, body digest, path,
  HTTP status) and the verification result computed by ``request_auth.verify_response``.
  ``verified`` requires key id and signature (constraint). Nothing is backfilled: historical
  outcomes have no recorded signature and are exported as not representable, never as evidence.
* ``v11_telemetry_windows``: windows measured by the v1.1 telemetry producer, unique per
  (experiment, source, sequence) and per (experiment, source, window_start).
* ``v11_delivery_state`` / ``v11_dead_letters``: at-least-once delivery cursors and
  permanently refused payloads.

Downgrade removes exactly these objects. It never touches ledger rows.
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("transactions", sa.Column("experiment_id", sa.String(length=80), nullable=True))
    op.add_column(
        "transactions", sa.Column("check_in_flight_id", sa.String(length=80), nullable=True)
    )
    op.create_index("ix_transactions_experiment_id", "transactions", ["experiment_id"])

    op.create_table(
        "evidence_records",
        sa.Column("evidence_id", sa.String(length=80), primary_key=True),
        sa.Column("transaction_id", sa.String(length=80), nullable=False),
        sa.Column("evidence_kind", sa.String(length=40), nullable=False),
        sa.Column("issuer_id", sa.String(length=16), nullable=False),
        sa.Column("path", sa.String(length=80), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=False),
        sa.Column("reported_status", sa.String(length=16), nullable=True),
        sa.Column("reported_transaction_id", sa.String(length=80), nullable=True),
        sa.Column("body_sha256", sa.String(length=64), nullable=False),
        sa.Column("response_timestamp", sa.String(length=20), nullable=True),
        sa.Column("key_id", sa.String(length=64), nullable=True),
        sa.Column("signature_hex", sa.String(length=128), nullable=True),
        sa.Column("signature_alg", sa.String(length=32), nullable=False),
        sa.Column("signed_fields", sa.JSON(), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.Column("verifier_id", sa.String(length=40), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("check_id", sa.String(length=80), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "NOT verified OR (key_id IS NOT NULL AND signature_hex IS NOT NULL)",
            name="ck_evidence_verified_has_signature",
        ),
        sa.CheckConstraint(
            "evidence_kind IN ('ORIGINAL_PAYMENT_RESPONSE','STATUS_CHECK_RESPONSE',"
            "'RECONCILIATION_RECORD')",
            name="ck_evidence_kind",
        ),
    )
    op.create_index("ix_evidence_records_transaction_id", "evidence_records", ["transaction_id"])
    op.create_index("ix_evidence_records_check_id", "evidence_records", ["check_id"])

    op.create_table(
        "v11_telemetry_windows",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("experiment_id", sa.String(length=80), nullable=False),
        sa.Column("source_id", sa.String(length=80), nullable=False),
        sa.Column("route_id", sa.String(length=20), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("measurement", sa.JSON(), nullable=False),
        sa.Column("verdict", sa.String(length=16), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("cross_check", sa.String(length=16), nullable=False),
        sa.Column("validator_id", sa.String(length=40), nullable=False),
        sa.Column("validator_version", sa.String(length=40), nullable=False),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.UniqueConstraint("experiment_id", "source_id", "sequence", name="uq_v11_window_seq"),
        sa.UniqueConstraint(
            "experiment_id", "source_id", "window_start", name="uq_v11_window_start"
        ),
        sa.CheckConstraint("window_end > window_start", name="ck_v11_window_positive"),
        sa.CheckConstraint("sequence >= 0", name="ck_v11_window_sequence"),
    )

    op.create_table(
        "v11_delivery_state",
        sa.Column("stream", sa.String(length=40), primary_key=True),
        sa.Column("last_id", sa.BigInteger(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "v11_dead_letters",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("stream", sa.String(length=40), nullable=False),
        sa.Column("ref_id", sa.String(length=80), nullable=False),
        sa.Column("error_code", sa.String(length=40), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("stream", "ref_id", name="uq_v11_dead_letter"),
    )


def downgrade() -> None:
    op.drop_table("v11_dead_letters")
    op.drop_table("v11_delivery_state")
    op.drop_table("v11_telemetry_windows")
    op.drop_index("ix_evidence_records_check_id", table_name="evidence_records")
    op.drop_index("ix_evidence_records_transaction_id", table_name="evidence_records")
    op.drop_table("evidence_records")
    op.drop_index("ix_transactions_experiment_id", table_name="transactions")
    op.drop_column("transactions", "check_in_flight_id")
    op.drop_column("transactions", "experiment_id")
