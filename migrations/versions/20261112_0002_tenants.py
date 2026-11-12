"""tenants, tenant integrations, tenant mapping configs; FK from sync_records

Additive:
- creates three new tables;
- backfills ``tenants`` from tenant ids already present in ``sync_records``
  (INSERT ... ON CONFLICT DO NOTHING, so existing history keeps a valid parent);
- adds a foreign key from ``sync_records.tenant_id`` using NOT VALID followed by
  VALIDATE CONSTRAINT, so validation doesn't block writes for the full scan.

No existing rows are modified or removed.

Constraint names are wrapped in ``op.f()`` so Alembic uses them verbatim instead
of applying the metadata naming convention a second time (see 0003).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-12
"""
import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_ID = r"~ '^[a-z0-9][a-z0-9-]{0,62}$'"
SECRET_REF = r"~ '^env:IL_[A-Z0-9_]{1,120}$'"


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(63), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'active'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenants")),
        sa.CheckConstraint(f"id {TENANT_ID}", name=op.f("ck_tenants_id_format")),
        sa.CheckConstraint("status IN ('active', 'suspended')", name=op.f("ck_tenants_status_valid")),
        sa.CheckConstraint("length(display_name) BETWEEN 1 AND 200", name=op.f("ck_tenants_display_name_length")),
    )

    op.create_table(
        "tenant_integrations",
        sa.Column("tenant_id", sa.String(63), nullable=False),
        sa.Column("crm_base_url", sa.String(500), nullable=False),
        sa.Column("crm_token_url", sa.String(500), nullable=False),
        sa.Column("crm_client_id", sa.String(200), nullable=False),
        sa.Column("crm_client_secret_ref", sa.String(128), nullable=False),
        sa.Column("cms_base_url", sa.String(500), nullable=False),
        sa.Column("cms_api_key_ref", sa.String(128), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("tenant_id", name=op.f("pk_tenant_integrations")),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT",
                                name=op.f("fk_tenant_integrations_tenant_id_tenants")),
        sa.CheckConstraint("crm_base_url ~ '^https?://'", name=op.f("ck_tenant_integrations_crm_base_url_scheme")),
        sa.CheckConstraint("crm_token_url ~ '^https?://'", name=op.f("ck_tenant_integrations_crm_token_url_scheme")),
        sa.CheckConstraint("cms_base_url ~ '^https?://'", name=op.f("ck_tenant_integrations_cms_base_url_scheme")),
        sa.CheckConstraint(f"crm_client_secret_ref {SECRET_REF}",
                           name=op.f("ck_tenant_integrations_crm_secret_ref_format")),
        sa.CheckConstraint(f"cms_api_key_ref {SECRET_REF}", name=op.f("ck_tenant_integrations_cms_key_ref_format")),
    )

    op.create_table(
        "tenant_mapping_configs",
        sa.Column("tenant_id", sa.String(63), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("tenant_id", "version", name=op.f("pk_tenant_mapping_configs")),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT",
                                name=op.f("fk_tenant_mapping_configs_tenant_id_tenants")),
        sa.CheckConstraint("version >= 1", name=op.f("ck_tenant_mapping_configs_version_positive")),
        sa.CheckConstraint("jsonb_typeof(config) = 'object'", name=op.f("ck_tenant_mapping_configs_config_is_object")),
        sa.CheckConstraint("config->>'tenant_id' = tenant_id",
                           name=op.f("ck_tenant_mapping_configs_config_tenant_matches")),
        sa.CheckConstraint("(config->>'version')::int = version",
                           name=op.f("ck_tenant_mapping_configs_config_version_matches")),
    )

    # Backfill: every tenant that already has history gets a tenants row.
    op.execute(
        "INSERT INTO tenants (id, display_name) "
        "SELECT DISTINCT tenant_id, tenant_id FROM sync_records "
        "ON CONFLICT (id) DO NOTHING"
    )

    op.execute(
        "ALTER TABLE sync_records ADD CONSTRAINT fk_sync_records_tenant_id_tenants "
        "FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE RESTRICT NOT VALID"
    )
    op.execute("ALTER TABLE sync_records VALIDATE CONSTRAINT fk_sync_records_tenant_id_tenants")


def downgrade() -> None:
    # Drops tenant configuration tables (data loss). Require explicit opt-in.
    if os.getenv("ALLOW_DESTRUCTIVE_DOWNGRADE") != "1":
        raise RuntimeError(
            "Downgrading 0002 drops tenants, tenant_integrations and tenant_mapping_configs. "
            "Set ALLOW_DESTRUCTIVE_DOWNGRADE=1 only with explicit approval.")
    op.drop_constraint("fk_sync_records_tenant_id_tenants", "sync_records", type_="foreignkey")
    op.drop_table("tenant_mapping_configs")
    op.drop_table("tenant_integrations")
    op.drop_table("tenants")
