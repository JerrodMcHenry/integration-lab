"""FastAPI dependencies: wiring of services and request context."""

from __future__ import annotations

import logging
from functools import lru_cache

from sqlalchemy import Engine

from app.core.config import CRMSettings, InternalSystemSettings, Settings, get_settings
from app.db.engine import build_engine
from app.integrations.crm_client import CRMClient
from app.integrations.internal_client import InternalSystemClient
from app.mapping.mapper import MappingRegistry
from app.repositories.sync_history import (
    InMemorySyncHistoryRepository,
    PostgresSyncHistoryRepository,
    SyncHistoryRepository,
)
from app.services.account_sync import AccountSyncService
from app.services.locks import InProcessSyncLocks, PostgresAdvisorySyncLocks, SyncLockProvider

logger = logging.getLogger(__name__)


@lru_cache
def get_engine() -> Engine | None:
    settings = get_settings()
    if not settings.database.url:
        logger.warning(
            "DATABASE_URL not set: using IN-MEMORY sync history and IN-PROCESS locks. "
            "History is lost on restart and multiple workers are NOT serialised.")
        return None
    return build_engine(settings.database)


@lru_cache
def get_sync_history() -> SyncHistoryRepository:
    engine = get_engine()
    return PostgresSyncHistoryRepository(engine) if engine else InMemorySyncHistoryRepository()


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
    return AccountSyncService(
        crm=CRMClient(settings.crm),
        destination=InternalSystemClient(settings.internal),
        mappings=MappingRegistry(settings.mapping_config_dir),
        history=get_sync_history(),
        locks=build_lock_provider(settings, get_engine()),
    )


def get_current_tenant() -> str:
    """Resolve the caller's tenant.

    TEMPORARY (until Milestone 5): returns the configured single tenant. It
    deliberately does NOT read a tenant id from the path, query or headers. M5
    replaces this with tenant resolution from a verified OIDC access token.
    """
    return get_settings().default_tenant_id
