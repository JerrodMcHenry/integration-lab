"""Application wiring: the one place that constructs long-lived objects.

Plain Python (no FastAPI), so the API, the worker and the CLI build the same
objects the same way. Objects are created on first use and cached for the life
of the process. FastAPI dependencies (``app/api/dependencies.py``) and the worker
entry point (``app/worker/__main__.py``) delegate here.

PostgreSQL is required (settings refuse to load without ``DATABASE_URL``).
In-memory implementations exist only as test fakes in ``tests/support/fakes.py``.
"""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy import Engine

from app.auth.tokens import JWKSCache, TokenVerifier, http_jwks_fetcher
from app.core.config import Settings, destination_write_margin, get_settings, worst_case_sync_seconds
from app.db.engine import build_engine
from app.repositories.audit import PostgresAuditRepository
from app.repositories.sync_history import PostgresSyncHistoryRepository
from app.repositories.tenants import PostgresTenantRepository
from app.repositories.operations import PostgresOperationsRepository
from app.repositories.webhook_events import PostgresWebhookEventRepository
from app.services.account_sync import AccountSyncService
from app.services.locks import PostgresAdvisorySyncLocks
from app.services.secrets import EnvSecretResolver
from app.services.tenant_context import DbMappingProvider, TenantConnectionProvider
from app.webhooks.ingest import WebhookIngestor
from app.worker.retry_policy import RetryPolicy
from app.worker.runner import EventWorker, resolve_lease_seconds


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
@lru_cache
def engine() -> Engine:
    return build_engine(get_settings().database)


@lru_cache
def sync_history() -> PostgresSyncHistoryRepository:
    return PostgresSyncHistoryRepository(engine())


@lru_cache
def audit_repository() -> PostgresAuditRepository:
    return PostgresAuditRepository(engine())


@lru_cache
def tenant_repository() -> PostgresTenantRepository:
    return PostgresTenantRepository(engine())


@lru_cache
def webhook_events() -> PostgresWebhookEventRepository:
    return PostgresWebhookEventRepository(engine())


@lru_cache
def operations_repository() -> PostgresOperationsRepository:
    return PostgresOperationsRepository(engine())


# --------------------------------------------------------------------------- #
# Services
# --------------------------------------------------------------------------- #
def build_lock_provider(settings: Settings, engine: Engine) -> PostgresAdvisorySyncLocks:
    # The server-side backstop must never fire during a legitimate sync,
    # otherwise the lock would be released while the sync is still writing.
    max_hold = max(300.0, 2 * worst_case_sync_seconds(settings.crm, settings.internal))
    return PostgresAdvisorySyncLocks(
        engine,
        lock_timeout_seconds=settings.database.lock_timeout_seconds,
        max_hold_seconds=max_hold,
    )


@lru_cache
def sync_service() -> AccountSyncService:
    settings = get_settings()
    tenants = tenant_repository()
    return AccountSyncService(
        connections=TenantConnectionProvider(
            tenants, EnvSecretResolver(app_env=settings.environment),
            crm_defaults=settings.crm, cms_defaults=settings.internal,
            require_https=settings.is_production),
        mappings=DbMappingProvider(tenants),
        history=sync_history(),
        locks=build_lock_provider(settings, engine()),
    )


@lru_cache
def token_verifier() -> TokenVerifier:
    oidc = get_settings().oidc
    return TokenVerifier(oidc, JWKSCache(http_jwks_fetcher(oidc), ttl_seconds=oidc.jwks_cache_seconds))


def webhook_ingestor() -> WebhookIngestor:
    settings = get_settings()
    return WebhookIngestor(tenants=tenant_repository(), events=webhook_events(),
                           secrets=EnvSecretResolver(app_env=settings.environment), settings=settings.webhooks)


def build_worker(worker_id: str | None = None) -> EventWorker:
    settings = get_settings()
    return EventWorker(events=webhook_events(), operations=operations_repository(), service=sync_service(),
                       tenants=tenant_repository(),
                       lease_seconds=resolve_lease_seconds(settings),
                       batch_size=settings.worker.batch_size, max_attempts=settings.worker.max_attempts,
                       poll_interval=settings.worker.poll_interval_seconds, worker_id=worker_id,
                       retry_policy=RetryPolicy(settings.retry, settings.worker.max_attempts),
                       write_margin_seconds=destination_write_margin(settings))
