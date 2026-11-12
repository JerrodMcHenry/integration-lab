"""Tenants, integrations and mapping versions in PostgreSQL; migration 0002 backfill."""

from __future__ import annotations

import os
import threading
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from app.repositories.sync_history import PostgresSyncHistoryRepository
from app.repositories.tenants import (
    MappingVersionConflict,
    PostgresTenantRepository,
    Tenant,
    TenantIntegration,
    seed_postgres,
)
from app.schemas.sync import SyncStatus
from app.seed import load_seed
from app.services.account_sync import AccountSyncService
from app.services.locks import PostgresAdvisorySyncLocks
from app.services.secrets import EnvSecretResolver
from app.services.tenant_context import DbMappingProvider, TenantConnectionProvider
from tests.conftest import cms_settings, crm_settings
from tests.integration.conftest import ROOT
from tests.test_api_security import HARBOR_MAPPING, MAPPING
from tests.test_tenant_isolation import SECRETS, World


def integration(tenant: str, **overrides) -> TenantIntegration:
    up = tenant.upper()
    values = dict(tenant_id=tenant, crm_base_url=f"http://crm-{tenant}.test",
                  crm_token_url=f"http://crm-{tenant}.test/oauth/token", crm_client_id=f"{tenant}-client",
                  crm_client_secret_ref=f"env:IL_{up}__CRM_CLIENT_SECRET", cms_base_url=f"http://cms-{tenant}.test",
                  cms_api_key_ref=f"env:IL_{up}__CMS_API_KEY")
    values.update(overrides)
    return TenantIntegration(**values)


@pytest.fixture
def repo(pg_engine):
    return PostgresTenantRepository(pg_engine)


# --------------------------------------------------------------------------- #
# Repository behaviour and isolation
# --------------------------------------------------------------------------- #
def test_tenant_lookup(repo, pg_engine):
    assert repo.get_tenant("meridian") == Tenant("meridian", "meridian", "active")
    assert repo.get_tenant("ghost") is None
    with pg_engine.begin() as c:
        c.execute(text("UPDATE tenants SET status = 'suspended' WHERE id = 'harbor'"))
    assert repo.get_tenant("harbor").active is False


def test_integrations_and_mappings_are_tenant_scoped(repo, pg_engine):
    seed_postgres(pg_engine, Tenant("meridian", "M", "active"), integration("meridian"), MAPPING)
    seed_postgres(pg_engine, Tenant("harbor", "H", "active"), integration("harbor"), HARBOR_MAPPING)
    assert repo.get_integration("harbor").crm_base_url == "http://crm-harbor.test"
    assert repo.get_integration("meridian").crm_base_url == "http://crm-meridian.test"
    assert repo.get_integration("other") is None
    assert repo.get_active_mapping("harbor").config["tenant_id"] == "harbor"
    repo.add_mapping_version("meridian", MAPPING, "admin@meridian")
    assert [v.version for v in repo.list_mapping_versions("meridian")] == [2, 1]
    assert [v.version for v in repo.list_mapping_versions("harbor")] == [1]
    assert repo.list_mapping_versions("other") == []


def test_mapping_versions_are_append_only_and_server_numbered(repo):
    v1 = repo.add_mapping_version("meridian", {**MAPPING, "version": 99}, "a")
    v2 = repo.add_mapping_version("meridian", MAPPING, "b")
    assert (v1.version, v1.config["version"]) == (1, 1)
    assert (v2.version, v2.created_by) == (2, "b")
    assert repo.get_active_mapping("meridian").version == 2


def test_concurrent_mapping_publish_conflicts_instead_of_overwriting(repo, pg_engine):
    """Writer A inserts v1 and holds its transaction; writer B computes the same next version."""
    repo.add_mapping_version("meridian", MAPPING, "seed")  # v1 committed
    conn = pg_engine.connect()
    txn = conn.begin()
    conn.execute(text("INSERT INTO tenant_mapping_configs (tenant_id, version, config, created_by) "
                      "VALUES ('meridian', 2, CAST(:c AS jsonb), 'writer-a')"),
                 {"c": __import__("json").dumps({**MAPPING, "version": 2})})
    result: dict = {}

    def writer_b():
        try:
            result["v"] = repo.add_mapping_version("meridian", MAPPING, "writer-b")
        except Exception as exc:  # noqa: BLE001
            result["err"] = exc

    t = threading.Thread(target=writer_b)
    t.start()
    t.join(0.5)            # B is blocked on A's uncommitted v2
    assert t.is_alive()
    txn.commit()
    conn.close()
    t.join(5)
    assert isinstance(result.get("err"), MappingVersionConflict)
    assert [v.created_by for v in repo.list_mapping_versions("meridian")] == ["writer-a", "seed"]


@pytest.mark.parametrize("sql, constraint", [
    ("INSERT INTO tenants (id, display_name) VALUES ('Bad_Id', 'x')", "ck_tenants_id_format"),
    ("INSERT INTO tenants (id, display_name, status) VALUES ('zz', 'x', 'deleted')", "ck_tenants_status_valid"),
    ("INSERT INTO tenant_mapping_configs (tenant_id, version, config, created_by) "
     "VALUES ('harbor', 1, '{\"tenant_id\": \"meridian\", \"version\": 1}', 'x')", "config_tenant_matches"),
    ("INSERT INTO tenant_mapping_configs (tenant_id, version, config, created_by) "
     "VALUES ('harbor', 1, '{\"tenant_id\": \"harbor\", \"version\": 7}', 'x')", "config_version_matches"),
    ("INSERT INTO tenant_mapping_configs (tenant_id, version, config, created_by) "
     "VALUES ('ghost', 1, '{\"tenant_id\": \"ghost\", \"version\": 1}', 'x')", "fk_tenant_mapping_configs"),
    ("INSERT INTO tenant_integrations VALUES ('harbor', 'http://a', 'http://a/t', 'c', "
     "'IL_HARBOR__X', 'http://b', 'env:IL_HARBOR__Y', now())", "crm_secret_ref_format"),
    ("INSERT INTO tenant_integrations VALUES ('harbor', 'file:///etc/passwd', 'http://a/t', 'c', "
     "'env:IL_HARBOR__X', 'http://b', 'env:IL_HARBOR__Y', now())", "crm_base_url_scheme"),
    ("INSERT INTO sync_records (id, tenant_id, crm_account_id, status, started_at, finished_at, duration_ms) "
     "VALUES (gen_random_uuid(), 'ghost', 'SF-1', 'created', now(), now(), 0)", "fk_sync_records_tenant_id"),
])
def test_tenant_constraints(pg_engine, sql, constraint):
    with pytest.raises(IntegrityError) as exc_info, pg_engine.begin() as c:
        c.execute(text(sql))
    assert constraint in str(exc_info.value.orig)


def test_tenant_with_history_cannot_be_deleted(pg_engine):
    from tests.integration.test_pg_history import rec
    PostgresSyncHistoryRepository(pg_engine).add(rec())
    with pytest.raises(IntegrityError, match="fk_sync_records_tenant_id"), pg_engine.begin() as c:
        c.execute(text("DELETE FROM tenants WHERE id = 'meridian'"))


def test_seed_is_idempotent_and_never_overwrites(pg_engine, repo):
    items = load_seed()
    first = [seed_postgres(pg_engine, i.tenant, i.integration, i.mapping) for i in items]
    with pg_engine.begin() as c:  # an operator customises a seeded row
        c.execute(text("UPDATE tenants SET display_name = 'Meridian (renamed)' WHERE id = 'meridian'"))
    second = [seed_postgres(pg_engine, i.tenant, i.integration, i.mapping) for i in items]
    assert first[0] == {"tenant": False, "integration": True, "mapping": True}  # tenant row pre-existed
    assert all(not any(s.values()) for s in second)
    assert repo.get_tenant("meridian").display_name == "Meridian (renamed)"
    assert repo.get_integration("harbor").crm_client_secret_ref == "env:IL_HARBOR__CRM_CLIENT_SECRET"


# --------------------------------------------------------------------------- #
# End to end: DB-backed tenants, mappings, connections, locks and history
# --------------------------------------------------------------------------- #
def test_two_tenants_end_to_end_with_postgres(pg_engine, repo):
    seed_postgres(pg_engine, Tenant("meridian", "M", "active"), integration("meridian"), MAPPING)
    seed_postgres(pg_engine, Tenant("harbor", "H", "active"), integration("harbor"), HARBOR_MAPPING)
    world = World()  # in-process CRM/CMS per tenant
    history = PostgresSyncHistoryRepository(pg_engine)
    service = AccountSyncService(
        connections=TenantConnectionProvider(repo, EnvSecretResolver(SECRETS, app_env="test"),
                                             crm_defaults=crm_settings(), cms_defaults=cms_settings(),
                                             http_client_factory=world.route),
        mappings=DbMappingProvider(repo), history=history,
        locks=PostgresAdvisorySyncLocks(pg_engine, lock_timeout_seconds=5))

    m = service.sync_account("meridian", "SF-1001")
    h = service.sync_account("harbor", "HB-2001")
    cross = service.sync_account("harbor", "SF-1001")

    assert (m.status, h.status) == (SyncStatus.CREATED, SyncStatus.CREATED)
    assert cross.error.code == "crm_not_found"
    assert [r.id for r in history.list_recent("meridian")] == [m.id]
    assert [r.id for r in history.list_recent("harbor")] == [cross.id, h.id]
    assert history.get("harbor", m.id) is None


# --------------------------------------------------------------------------- #
# Migration 0002 backfill (in a throwaway schema inside the _test database)
# --------------------------------------------------------------------------- #
def test_migration_0002_backfills_tenants_from_existing_history(pg_url):
    schema = f"mig_{uuid.uuid4().hex[:10]}"
    admin = create_engine(pg_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped_url = f"{pg_url}?options=-csearch_path%3D{schema}"
    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT, "migrations"))
    cfg.set_main_option("sqlalchemy.url", scoped_url.replace("%", "%%"))
    scoped = create_engine(scoped_url)
    try:
        command.upgrade(cfg, "0001")
        with scoped.begin() as c:  # history written before tenants existed
            for tenant in ("legacy-a", "legacy-a", "legacy-b"):
                c.execute(text("INSERT INTO sync_records (id, tenant_id, crm_account_id, status, started_at, "
                               "finished_at, duration_ms) VALUES (gen_random_uuid(), :t, 'X', 'created', now(), "
                               "now(), 0)"), {"t": tenant})
        command.upgrade(cfg, "head")
        with scoped.connect() as c:
            tenants = c.execute(text("SELECT id, display_name, status FROM tenants ORDER BY id")).all()
            assert c.execute(text("SELECT count(*) FROM sync_records")).scalar() == 3  # nothing lost
            validated = c.execute(text(
                "SELECT convalidated FROM pg_constraint WHERE conname = 'fk_sync_records_tenant_id_tenants' "
                "AND connamespace = CAST(:s AS regnamespace)"), {"s": schema}).scalar()
        assert tenants == [("legacy-a", "legacy-a", "active"), ("legacy-b", "legacy-b", "active")]
        assert validated is True
    finally:
        scoped.dispose()
        with admin.connect() as c:
            # Only the schema this test just created inside the _test database.
            c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
