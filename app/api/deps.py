"""FastAPI dependencies: wiring of services and request context."""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Annotated

from fastapi import Depends
from sqlalchemy import Engine

from app.auth.dependencies import get_principal
from app.auth.tokens import Principal
from app.core.config import CRMSettings, InternalSystemSettings, Settings, get_settings
from app.db.engine import build_engine
from app.repositories.sync_history import (
    InMemorySyncHistoryRepository,
    PostgresSyncHistoryRepository,
    SyncHistoryRepository,
)
from app.repositories.tenants import (
    InMemoryTenantRepository,
    PostgresTenantRepository,
    TenantRepository,
    seed_memory,
)
from app.services.account_sync import AccountSyncService
from app.services.locks import InProcessSyncLocks, PostgresAdvisorySyncLocks, SyncLockProvider
from app.services.secrets import EnvSecretResolver
from app.services.tenant_context import DbMappingProvider, TenantConnectionProvider

logger = logging.getLogger(__name__)


@lru_cache
def get_engine() -> Engine | None:
    settings = get_settings()
    if not settings.database.url:
        logger.warning(
            "DATABASE_URL not set: using IN-MEMORY history/tenants (seeded from seed/dev_tenants.json) "
            "and IN-PROCESS locks. Data is lost on restart and multiple workers are NOT serialised.")
        return None
    return build_engine(settings.database)


@lru_cache
def get_sync_history() -> SyncHistoryRepository:
    engine = get_engine()
    return PostgresSyncHistoryRepository(engine) if engine else InMemorySyncHistoryRepository()


@lru_cache
def get_tenant_repository() -> TenantRepository:
    engine = get_engine()
    if engine:
        return PostgresTenantRepository(engine)
    from app.seed import load_seed

    repo = InMemoryTenantRepository()
    for item in load_seed():
        seed_memory(repo, item.tenant, item.integration, item.mapping)
    return repo


def worst_case_sync_seconds(crm: CRMSettings, internal: InternalSystemSettings) -> float:
    """Upper bound on how long one sync can hold its lock.

    CRM: token fetch + account fetch with retries. CMS: up to 3 calls with
    retries (lookup, create or conflict re-lookup, update).
    """
    def call(timeout: float, retries: int, cap: float) -> float:
        return (retries + 1) * timeout + retries * cap

    crm_s = crm.timeout_seconds + call(crm.timeout_seconds, crm.max_retries, crm.backoff_max_seconds)
    cms_s = 3 * call(internal.timeout_seconds, internal.max_retries, internal.backoff_max_seconds)
    return crm_s + cms_s


def build_lock_provider(settings: Settings, engine: Engine | None) -> SyncLockProvider:
    if engine is None:
        return InProcessSyncLocks()
    # The server-side backstop must never fire during a legitimate sync,
    # otherwise the lock would be released while the sync is still writing.
    max_hold = max(300.0, 2 * worst_case_sync_seconds(settings.crm, settings.internal))
    return PostgresAdvisorySyncLocks(
        engine,
        lock_timeout_seconds=settings.database.lock_timeout_seconds,
        max_hold_seconds=max_hold,
    )


@lru_cache
def get_sync_service() -> AccountSyncService:
    settings = get_settings()
    tenants = get_tenant_repository()
    return AccountSyncService(
        connections=TenantConnectionProvider(
            tenants, EnvSecretResolver(app_env=settings.environment),
            crm_defaults=settings.crm, cms_defaults=settings.internal),
        mappings=DbMappingProvider(tenants),
        history=get_sync_history(),
        locks=build_lock_provider(settings, get_engine()),
    )


def get_current_tenant(principal: Annotated[Principal, Depends(get_principal)]) -> str:
    """The caller's tenant, taken only from the verified access token."""
    return principal.tenant_id
