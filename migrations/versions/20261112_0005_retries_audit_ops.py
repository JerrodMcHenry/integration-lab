"""retries + dead letter, attempt history, audit log, worker heartbeats

Additive:
- ``webhook_events``: new status ``dead_letter``; columns ``dead_lettered_at``,
  ``replay_count``, ``last_replayed_at``, ``last_replayed_by``. Two CHECK
  constraints are swapped for wider versions (DDL only; existing rows are
  re-validated, none change);
- ``webhook_event_attempts``: one row per processing attempt (retry history);
- ``audit_log``: append-only (a trigger blocks UPDATE and DELETE);
- ``worker_heartbeats``: liveness of worker processes.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-12
"""
import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = "'pending', 'processing', 'succeeded', 'failed', 'dead_letter', 'ignored'"
TERMINAL = "'succeeded', 'failed', 'dead_letter', 'ignored'"
ATTEMPT_OUTCOMES = "'succeeded', 'retry_scheduled', 'failed', 'dead_letter', 'lease_expired'"


def upgrade() -> None:
    # --- webhook_events: dead letter + replay bookkeeping ---------------------
    op.add_column("webhook_events", sa.Column("dead_lettered_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("webhook_events", sa.Column("replay_count", sa.Integer(), nullable=False, server_default=sa.text("0")))
    op.add_column("webhook_events", sa.Column("last_replayed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("webhook_events", sa.Column("last_replayed_by", sa.String(255), nullable=True))
    for name, expr in [
        ("status_valid", f"status IN ({STATUSES})"),
        ("terminal_has_processed_at", f"status NOT IN ({TERMINAL}) OR processed_at IS NOT NULL"),
        ("failed_has_error", "status NOT IN ('failed', 'dead_letter') OR last_error_code IS NOT NULL"),
    ]:
        op.drop_constraint(op.f(f"ck_webhook_events_{name}"), "webhook_events", type_="check")
        op.create_check_constraint(op.f(f"ck_webhook_events_{name}"), "webhook_events", expr)
    op.create_check_constraint(op.f("ck_webhook_events_replay_count_non_negative"), "webhook_events",
                               "replay_count >= 0")
    op.create_check_constraint(op.f("ck_webhook_events_dead_letter_has_timestamp"), "webhook_events",
                               "status <> 'dead_letter' OR dead_lettered_at IS NOT NULL")

    # --- attempt history -------------------------------------------------------
    op.create_table(
        "webhook_event_attempts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("event_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(63), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(128), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("outcome", sa.String(20), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sync_record_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_event_attempts")),
        sa.ForeignKeyConstraint(["event_uuid"], ["webhook_events.id"], ondelete="CASCADE",
                                name=op.f("fk_webhook_event_attempts_event_uuid_webhook_events")),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT",
                                name=op.f("fk_webhook_event_attempts_tenant_id_tenants")),
        sa.CheckConstraint(f"outcome IN ({ATTEMPT_OUTCOMES})", name=op.f("ck_webhook_event_attempts_outcome_valid")),
        sa.CheckConstraint("attempt >= 1", name=op.f("ck_webhook_event_attempts_attempt_positive")),
        sa.CheckConstraint("error_message IS NULL OR length(error_message) <= 1000",
                           name=op.f("ck_webhook_event_attempts_error_message_length")),
    )
    op.create_index("ix_webhook_event_attempts_event", "webhook_event_attempts", ["event_uuid", "attempt"])
    op.create_index("ix_webhook_event_attempts_tenant_recent", "webhook_event_attempts",
                    ["tenant_id", sa.text("finished_at DESC")])

    # --- audit log (append-only) ---------------------------------------------
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("tenant_id", sa.String(63), nullable=True),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("actor_client_id", sa.String(255), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(64), nullable=True),
        sa.Column("target_id", sa.String(128), nullable=True),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT",
                                name=op.f("fk_audit_log_tenant_id_tenants")),
        sa.CheckConstraint("outcome IN ('success', 'denied', 'rejected', 'error')",
                           name=op.f("ck_audit_log_outcome_valid")),
        sa.CheckConstraint("action ~ '^[a-z_]+(\\.[a-z_]+)+$'", name=op.f("ck_audit_log_action_format")),
        sa.CheckConstraint("jsonb_typeof(details) = 'object'", name=op.f("ck_audit_log_details_is_object")),
    )
    op.create_index("ix_audit_log_tenant_recent", "audit_log", ["tenant_id", sa.text("occurred_at DESC")])
    op.execute("""
        CREATE FUNCTION audit_log_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log is append-only (% blocked)', TG_OP USING ERRCODE = 'insufficient_privilege';
        END $$
    """)
    op.execute("CREATE TRIGGER audit_log_no_update_delete BEFORE UPDATE OR DELETE ON audit_log "
               "FOR EACH ROW EXECUTE FUNCTION audit_log_immutable()")
    op.execute("CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log "
               "FOR EACH STATEMENT EXECUTE FUNCTION audit_log_immutable()")

    # --- worker heartbeats -----------------------------------------------------
    op.create_table(
        "worker_heartbeats",
        sa.Column("worker_id", sa.String(128), nullable=False),
        sa.Column("hostname", sa.String(255), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("events_succeeded", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("events_failed", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("current_event_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint("worker_id", name=op.f("pk_worker_heartbeats")),
        sa.CheckConstraint("state IN ('idle', 'processing', 'backing_off', 'stopped')",
                           name=op.f("ck_worker_heartbeats_state_valid")),
    )


def downgrade() -> None:
    if os.getenv("ALLOW_DESTRUCTIVE_DOWNGRADE") != "1":
        raise RuntimeError("Downgrading 0005 drops audit_log, attempt history and heartbeats. "
                           "Set ALLOW_DESTRUCTIVE_DOWNGRADE=1 only with explicit approval.")
    op.drop_table("worker_heartbeats")
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log")
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_update_delete ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_immutable()")
    op.drop_table("audit_log")
    op.drop_table("webhook_event_attempts")
    for name in ("dead_letter_has_timestamp", "replay_count_non_negative"):
        op.drop_constraint(op.f(f"ck_webhook_events_{name}"), "webhook_events", type_="check")
    for col in ("last_replayed_by", "last_replayed_at", "replay_count", "dead_lettered_at"):
        op.drop_column("webhook_events", col)
