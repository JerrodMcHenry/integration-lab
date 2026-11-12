"""webhook_events inbox; webhook secret references on tenant_integrations

Additive:
- adds nullable ``webhook_secret_ref`` / ``webhook_secret_previous_ref`` to
  ``tenant_integrations`` and fills ``webhook_secret_ref`` on existing rows with
  the conventional reference ``env:IL_<TENANT>__WEBHOOK_SECRET`` (a pointer to an
  env var in the tenant's own namespace, not a secret). If that env var isn't
  set, webhooks for the tenant are rejected (fail closed);
- creates ``webhook_events`` with a tenant-scoped unique constraint for
  durable idempotency and partial indexes for worker claiming and lease recovery.

No existing data is removed. Constraint names use ``op.f()`` (see 0003).

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-12
"""
import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SECRET_REF = r"~ '^env:IL_[A-Z0-9_]{1,120}$'"
STATUSES = "'pending', 'processing', 'succeeded', 'failed', 'ignored'"


def upgrade() -> None:
    op.add_column("tenant_integrations", sa.Column("webhook_secret_ref", sa.String(128), nullable=True))
    op.add_column("tenant_integrations", sa.Column("webhook_secret_previous_ref", sa.String(128), nullable=True))
    op.create_check_constraint(op.f("ck_tenant_integrations_webhook_secret_ref_format"), "tenant_integrations",
                               f"webhook_secret_ref IS NULL OR webhook_secret_ref {SECRET_REF}")
    op.create_check_constraint(op.f("ck_tenant_integrations_webhook_secret_previous_ref_format"),
                               "tenant_integrations",
                               f"webhook_secret_previous_ref IS NULL OR webhook_secret_previous_ref {SECRET_REF}")
    op.execute(
        "UPDATE tenant_integrations "
        "SET webhook_secret_ref = 'env:IL_' || upper(replace(tenant_id, '-', '_')) || '__WEBHOOK_SECRET' "
        "WHERE webhook_secret_ref IS NULL"
    )

    op.create_table(
        "webhook_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(63), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("event_id", sa.String(128), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("crm_account_id", sa.String(64), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("claim_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("claimed_by", sa.String(128), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column("last_error_retryable", sa.Boolean(), nullable=True),
        sa.Column("sync_record_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_events")),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT",
                                name=op.f("fk_webhook_events_tenant_id_tenants")),
        sa.ForeignKeyConstraint(["sync_record_id"], ["sync_records.id"], ondelete="RESTRICT",
                                name=op.f("fk_webhook_events_sync_record_id_sync_records")),
        sa.UniqueConstraint("tenant_id", "source", "event_id",
                            name=op.f("uq_webhook_events_tenant_id_source_event_id")),
        sa.CheckConstraint("source IN ('crm')", name=op.f("ck_webhook_events_source_valid")),
        sa.CheckConstraint("length(event_id) BETWEEN 1 AND 128", name=op.f("ck_webhook_events_event_id_length")),
        sa.CheckConstraint(f"status IN ({STATUSES})", name=op.f("ck_webhook_events_status_valid")),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_webhook_events_attempts_non_negative")),
        sa.CheckConstraint("payload_sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_webhook_events_payload_sha256_format")),
        sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name=op.f("ck_webhook_events_payload_is_object")),
        sa.CheckConstraint("(status = 'processing') = (claim_token IS NOT NULL AND lease_expires_at IS NOT NULL)",
                           name=op.f("ck_webhook_events_lease_iff_processing")),
        sa.CheckConstraint("status NOT IN ('succeeded', 'failed', 'ignored') OR processed_at IS NOT NULL",
                           name=op.f("ck_webhook_events_terminal_has_processed_at")),
        sa.CheckConstraint("status <> 'failed' OR last_error_code IS NOT NULL",
                           name=op.f("ck_webhook_events_failed_has_error")),
        sa.CheckConstraint("last_error_message IS NULL OR length(last_error_message) <= 1000",
                           name=op.f("ck_webhook_events_error_message_length")),
    )
    op.create_index("ix_webhook_events_claimable", "webhook_events", ["available_at", "received_at"],
                    postgresql_where=sa.text("status = 'pending'"))
    op.create_index("ix_webhook_events_expired_leases", "webhook_events", ["lease_expires_at"],
                    postgresql_where=sa.text("status = 'processing'"))
    op.create_index("ix_webhook_events_tenant_recent", "webhook_events", ["tenant_id", sa.text("received_at DESC")])


def downgrade() -> None:
    if os.getenv("ALLOW_DESTRUCTIVE_DOWNGRADE") != "1":
        raise RuntimeError("Downgrading 0004 drops webhook_events (the event inbox). "
                           "Set ALLOW_DESTRUCTIVE_DOWNGRADE=1 only with explicit approval.")
    op.drop_table("webhook_events")
    op.drop_constraint(op.f("ck_tenant_integrations_webhook_secret_previous_ref_format"), "tenant_integrations")
    op.drop_constraint(op.f("ck_tenant_integrations_webhook_secret_ref_format"), "tenant_integrations")
    op.drop_column("tenant_integrations", "webhook_secret_previous_ref")
    op.drop_column("tenant_integrations", "webhook_secret_ref")
