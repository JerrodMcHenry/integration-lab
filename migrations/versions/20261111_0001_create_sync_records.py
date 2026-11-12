"""create sync_records

Additive only: creates one table and its indexes. Touches no existing data.

Revision ID: 0001
Revises:
Create Date: 2026-11-11
"""
import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = "'created', 'updated', 'unchanged', 'skipped_stale', 'failed'"
STAGES = "'acquire_lock', 'fetch_source', 'transform', 'load_destination', 'internal'"


def upgrade() -> None:
    op.create_table(
        "sync_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("tenant_id", sa.String(63), nullable=False),
        sa.Column("crm_account_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("stage", sa.String(32), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=True),
        sa.Column("upstream_status", sa.SmallInteger(), nullable=True),
        sa.Column("error_issues", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
        sa.Column("destination_customer_id", sa.String(64), nullable=True),
        sa.Column("fields_changed", postgresql.ARRAY(sa.String(64)), nullable=False,
                  server_default=sa.text("'{}'::varchar[]")),
        sa.Column("mapping_version", sa.Integer(), nullable=True),
        sa.Column("source_last_modified", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name="pk_sync_records"),
        sa.UniqueConstraint("seq", name="uq_sync_records_seq"),
        sa.CheckConstraint(r"tenant_id ~ '^[a-z0-9][a-z0-9-]{0,62}$'",
                           name="ck_sync_records_tenant_id_format"),
        sa.CheckConstraint("length(crm_account_id) BETWEEN 1 AND 64",
                           name="ck_sync_records_crm_account_id_length"),
        sa.CheckConstraint(f"status IN ({STATUSES})", name="ck_sync_records_status_valid"),
        sa.CheckConstraint(f"stage IS NULL OR stage IN ({STAGES})", name="ck_sync_records_stage_valid"),
        sa.CheckConstraint(
            "(status = 'failed') = (stage IS NOT NULL AND error_code IS NOT NULL "
            "AND error_message IS NOT NULL AND retryable IS NOT NULL)",
            name="ck_sync_records_failure_fields_consistent"),
        sa.CheckConstraint(
            "status = 'failed' OR (stage IS NULL AND error_code IS NULL AND error_message IS NULL "
            "AND retryable IS NULL AND upstream_status IS NULL)",
            name="ck_sync_records_success_has_no_error"),
        sa.CheckConstraint("error_message IS NULL OR length(error_message) <= 1000",
                           name="ck_sync_records_error_message_length"),
        sa.CheckConstraint("upstream_status IS NULL OR upstream_status BETWEEN 100 AND 599",
                           name="ck_sync_records_upstream_status_range"),
        sa.CheckConstraint("jsonb_typeof(error_issues) = 'array'",
                           name="ck_sync_records_error_issues_is_array"),
        sa.CheckConstraint("mapping_version IS NULL OR mapping_version >= 1",
                           name="ck_sync_records_mapping_version_positive"),
        sa.CheckConstraint("finished_at >= started_at", name="ck_sync_records_finished_after_started"),
        sa.CheckConstraint("duration_ms >= 0", name="ck_sync_records_duration_non_negative"),
    )
    op.create_index("ix_sync_records_tenant_recent", "sync_records",
                    ["tenant_id", sa.text("seq DESC")])
    op.create_index("ix_sync_records_tenant_account_recent", "sync_records",
                    ["tenant_id", "crm_account_id", sa.text("seq DESC")])
    op.create_index("ix_sync_records_tenant_failed_recent", "sync_records",
                    ["tenant_id", sa.text("seq DESC")],
                    postgresql_where=sa.text("status = 'failed'"))


def downgrade() -> None:
    # Dropping the table destroys sync history. Require an explicit opt-in.
    if os.getenv("ALLOW_DESTRUCTIVE_DOWNGRADE") != "1":
        raise RuntimeError(
            "Downgrading 0001 drops sync_records and all history. "
            "Set ALLOW_DESTRUCTIVE_DOWNGRADE=1 only with explicit approval.")
    op.drop_index("ix_sync_records_tenant_failed_recent", table_name="sync_records")
    op.drop_index("ix_sync_records_tenant_account_recent", table_name="sync_records")
    op.drop_index("ix_sync_records_tenant_recent", table_name="sync_records")
    op.drop_table("sync_records")
