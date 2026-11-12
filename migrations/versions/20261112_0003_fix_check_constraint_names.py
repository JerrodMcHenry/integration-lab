"""normalize CHECK constraint names doubled by the naming convention

Migrations 0001 (and early builds of 0002) passed already-prefixed CHECK names
without ``op.f()``. Alembic then applied ``ck_%(table_name)s_%(constraint_name)s``
again, producing ``ck_sync_records_ck_sync_records_status_valid``. Names longer
than 63 characters were also truncated by SQLAlchemy to ``name[:55] + "_" + md5[-4:]``.

This migration renames any such constraint to its intended name. It is:
- non-destructive: ALTER TABLE ... RENAME CONSTRAINT only; no data or rules change;
- idempotent: renames only names that actually exist, so it's a no-op on a
  database created with the corrected migrations.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-12
"""
import hashlib
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MAX_IDENTIFIER = 63

INTENDED: dict[str, list[str]] = {
    "sync_records": [
        "tenant_id_format", "crm_account_id_length", "status_valid", "stage_valid",
        "failure_fields_consistent", "success_has_no_error", "error_message_length",
        "upstream_status_range", "error_issues_is_array", "mapping_version_positive",
        "finished_after_started", "duration_non_negative",
    ],
    "tenants": ["id_format", "status_valid", "display_name_length"],
    "tenant_integrations": [
        "crm_base_url_scheme", "crm_token_url_scheme", "cms_base_url_scheme",
        "crm_secret_ref_format", "cms_key_ref_format",
    ],
    "tenant_mapping_configs": [
        "version_positive", "config_is_object", "config_tenant_matches", "config_version_matches",
    ],
}


def _sqlalchemy_truncate(name: str) -> str:
    """Mirror SQLAlchemy's _truncate_and_render_maxlen_name for PostgreSQL."""
    if len(name) <= MAX_IDENTIFIER:
        return name
    return name[: MAX_IDENTIFIER - 8] + "_" + hashlib.md5(name.encode()).hexdigest()[-4:]


def upgrade() -> None:
    bind = op.get_bind()
    for table, shorts in INTENDED.items():
        existing = {row[0] for row in bind.execute(sa.text(
            "SELECT conname FROM pg_constraint WHERE contype = 'c' "
            "AND conrelid = to_regclass(:t)"), {"t": table})}
        for short in shorts:
            intended = f"ck_{table}_{short}"
            doubled = f"ck_{table}_{intended}"
            for old in (doubled, _sqlalchemy_truncate(doubled)):
                if old in existing and intended not in existing:
                    op.execute(f'ALTER TABLE {table} RENAME CONSTRAINT "{old}" TO "{intended}"')
                    existing.discard(old)
                    existing.add(intended)
                    break


def downgrade() -> None:
    # Renaming back would only reintroduce the defect; nothing to undo safely.
    pass
