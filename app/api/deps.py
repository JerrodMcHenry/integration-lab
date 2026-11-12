"""FastAPI dependencies: wiring of services and request context."""

from __future__ import annotations

from functools import lru_cache

from app.core.config import get_settings
from app.integrations.crm_client import CRMClient
from app.integrations.internal_client import InternalSystemClient
from app.mapping.mapper import MappingRegistry
from app.repositories.sync_history import InMemorySyncHistoryRepository, SyncHistoryRepository
from app.services.account_sync import AccountSyncService


@lru_cache
def get_sync_history() -> SyncHistoryRepository:
    # Replaced by a PostgreSQL repository in Milestone 4.
    return InMemorySyncHistoryRepository()


@lru_cache
def get_sync_service() -> AccountSyncService:
    settings = get_settings()
    return AccountSyncService(
        crm=CRMClient(settings.crm),
        destination=InternalSystemClient(settings.internal),
        mappings=MappingRegistry(settings.mapping_config_dir),
        history=get_sync_history(),
    )


def get_current_tenant() -> str:
    """Resolve the caller's tenant.

    TEMPORARY (until Milestone 5): returns the configured single tenant. It
    deliberately does NOT read a tenant id from the path, query or headers. M5
    replaces this with tenant resolution from a verified OIDC access token.
    """
    return get_settings().default_tenant_id
