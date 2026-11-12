"""Tenants, per-tenant integration settings, and versioned mapping configs.

Every read and write takes the tenant id as its first argument. There is no
method that lists or returns another tenant's integration or mapping data.
"""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy import Engine, func, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from app.db.models import TenantIntegrationRow, TenantMappingConfigRow, TenantRow
from app.repositories.sync_history import RepositoryError, _translate


@dataclass(frozen=True)
class Tenant:
    id: str
    display_name: str
    status: str  # "active" | "suspended"

    @property
    def active(self) -> bool:
        return self.status == "active"


@dataclass(frozen=True)
class TenantIntegration:
    tenant_id: str
    crm_base_url: str
    crm_token_url: str
    crm_client_id: str
    crm_client_secret_ref: str
    cms_base_url: str
    cms_api_key_ref: str
    webhook_secret_ref: str | None = None
    webhook_secret_previous_ref: str | None = None


@dataclass(frozen=True)
class MappingVersion:
    tenant_id: str
    version: int
    config: dict[str, Any]
    created_by: str
    created_at: datetime


class MappingVersionConflict(RepositoryError):
    """Another writer created the same version concurrently."""


class TenantRepository(Protocol):
    def get_tenant(self, tenant_id: str) -> Tenant | None: ...
    def get_integration(self, tenant_id: str) -> TenantIntegration | None: ...
    def get_active_mapping(self, tenant_id: str) -> MappingVersion | None: ...
    def list_mapping_versions(self, tenant_id: str) -> list[MappingVersion]: ...
    def add_mapping_version(self, tenant_id: str, config: dict[str, Any], created_by: str) -> MappingVersion: ...


# --------------------------------------------------------------------------- #
# In-memory
# --------------------------------------------------------------------------- #
class InMemoryTenantRepository:
    def __init__(self) -> None:
        self._tenants: dict[str, Tenant] = {}
        self._integrations: dict[str, TenantIntegration] = {}
        self._mappings: dict[str, list[MappingVersion]] = {}
        self._lock = threading.Lock()

    # seeding helpers (not part of the protocol)
    def put_tenant(self, tenant: Tenant) -> None:
        self._tenants[tenant.id] = tenant

    def put_integration(self, integration: TenantIntegration) -> None:
        self._integrations[integration.tenant_id] = integration

    def get_tenant(self, tenant_id: str) -> Tenant | None:
        return self._tenants.get(tenant_id)

    def get_integration(self, tenant_id: str) -> TenantIntegration | None:
        return self._integrations.get(tenant_id)

    def get_active_mapping(self, tenant_id: str) -> MappingVersion | None:
        versions = self._mappings.get(tenant_id)
        return copy.deepcopy(versions[-1]) if versions else None

    def list_mapping_versions(self, tenant_id: str) -> list[MappingVersion]:
        return copy.deepcopy(list(reversed(self._mappings.get(tenant_id, []))))

    def add_mapping_version(self, tenant_id: str, config: dict[str, Any], created_by: str) -> MappingVersion:
        with self._lock:
            if tenant_id not in self._tenants:
                raise RepositoryError("unknown tenant")
            versions = self._mappings.setdefault(tenant_id, [])
            version = (versions[-1].version if versions else 0) + 1
            stored = MappingVersion(tenant_id, version, {**copy.deepcopy(config), "version": version},
                                    created_by, datetime.now(timezone.utc))
            versions.append(stored)
            return copy.deepcopy(stored)


# --------------------------------------------------------------------------- #
# PostgreSQL
# --------------------------------------------------------------------------- #
class PostgresTenantRepository:
    def __init__(self, engine: Engine) -> None:
        self._sessions = sessionmaker(engine, expire_on_commit=False)

    def get_tenant(self, tenant_id: str) -> Tenant | None:
        row = self._one(select(TenantRow).where(TenantRow.id == tenant_id))
        return Tenant(row.id, row.display_name, row.status) if row else None

    def get_integration(self, tenant_id: str) -> TenantIntegration | None:
        row = self._one(select(TenantIntegrationRow).where(TenantIntegrationRow.tenant_id == tenant_id))
        if row is None:
            return None
        return TenantIntegration(row.tenant_id, row.crm_base_url, row.crm_token_url, row.crm_client_id,
                                 row.crm_client_secret_ref, row.cms_base_url, row.cms_api_key_ref,
                                 row.webhook_secret_ref, row.webhook_secret_previous_ref)

    def get_active_mapping(self, tenant_id: str) -> MappingVersion | None:
        row = self._one(
            select(TenantMappingConfigRow)
            .where(TenantMappingConfigRow.tenant_id == tenant_id)
            .order_by(TenantMappingConfigRow.version.desc()).limit(1))
        return _mapping(row) if row else None

    def list_mapping_versions(self, tenant_id: str) -> list[MappingVersion]:
        stmt = (select(TenantMappingConfigRow)
                .where(TenantMappingConfigRow.tenant_id == tenant_id)
                .order_by(TenantMappingConfigRow.version.desc()).limit(100))
        try:
            with self._sessions() as session:
                return [_mapping(r) for r in session.scalars(stmt).all()]
        except SQLAlchemyError as exc:
            raise _translate(exc) from exc

    def add_mapping_version(self, tenant_id: str, config: dict[str, Any], created_by: str) -> MappingVersion:
        try:
            with self._sessions.begin() as session:
                current = session.scalar(
                    select(func.coalesce(func.max(TenantMappingConfigRow.version), 0))
                    .where(TenantMappingConfigRow.tenant_id == tenant_id))
                version = current + 1
                row = TenantMappingConfigRow(tenant_id=tenant_id, version=version,
                                             config={**config, "version": version},
                                             created_by=created_by[:255])
                session.add(row)
                session.flush()
                return _mapping(row)
        except IntegrityError as exc:
            # PK (tenant_id, version) collision: a concurrent writer won. Caller may retry.
            raise MappingVersionConflict("mapping version conflict") from exc
        except SQLAlchemyError as exc:
            raise _translate(exc) from exc

    def _one(self, stmt):
        try:
            with self._sessions() as session:
                return session.scalars(stmt).one_or_none()
        except SQLAlchemyError as exc:
            raise _translate(exc) from exc


def _mapping(row: TenantMappingConfigRow) -> MappingVersion:
    return MappingVersion(row.tenant_id, row.version, row.config, row.created_by, row.created_at)


# --------------------------------------------------------------------------- #
# Seeding (idempotent, insert-if-absent; never overwrites)
# --------------------------------------------------------------------------- #
def seed_postgres(engine: Engine, tenant: Tenant, integration: TenantIntegration | None,
                  mapping: dict[str, Any] | None) -> dict[str, bool]:
    created = {"tenant": False, "integration": False, "mapping": False}
    with sessionmaker(engine).begin() as session:
        created["tenant"] = bool(session.execute(
            pg_insert(TenantRow).values(id=tenant.id, display_name=tenant.display_name, status=tenant.status)
            .on_conflict_do_nothing(index_elements=["id"]).returning(TenantRow.id)).first())
        if integration:
            created["integration"] = bool(session.execute(
                pg_insert(TenantIntegrationRow).values(**integration.__dict__)
                .on_conflict_do_nothing(index_elements=["tenant_id"])
                .returning(TenantIntegrationRow.tenant_id)).first())
        if mapping:
            has_any = session.scalar(select(func.count()).select_from(TenantMappingConfigRow)
                                     .where(TenantMappingConfigRow.tenant_id == tenant.id))
            if not has_any:
                session.execute(insert(TenantMappingConfigRow).values(
                    tenant_id=tenant.id, version=1, config={**mapping, "version": 1}, created_by="seed"))
                created["mapping"] = True
    return created


def seed_memory(repo: InMemoryTenantRepository, tenant: Tenant, integration: TenantIntegration | None,
                mapping: dict[str, Any] | None) -> None:
    repo.put_tenant(tenant)
    if integration:
        repo.put_integration(integration)
    if mapping and repo.get_active_mapping(tenant.id) is None:
        repo.add_mapping_version(tenant.id, mapping, "seed")


__all__ = ["Tenant", "TenantIntegration", "MappingVersion", "MappingVersionConflict", "TenantRepository",
           "InMemoryTenantRepository", "PostgresTenantRepository", "seed_postgres", "seed_memory"]
