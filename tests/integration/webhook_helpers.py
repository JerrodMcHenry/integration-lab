"""Shared helpers for webhook and worker integration tests."""

from __future__ import annotations

import json
import time
import uuid

from sqlalchemy import text

from app.core.config import WebhookSettings
from app.repositories.tenants import PostgresTenantRepository, Tenant, seed_postgres
from app.repositories.webhook_events import PostgresWebhookEventRepository
from app.services.account_sync import AccountSyncService
from app.services.locks import PostgresAdvisorySyncLocks
from app.services.secrets import EnvSecretResolver
from app.services.tenant_context import DbMappingProvider, TenantConnectionProvider
from app.webhooks.ingest import WebhookIngestor
from app.webhooks.signing import sign_header
from app.worker.runner import EventWorker
from tests.conftest import cms_settings, crm_settings
from tests.integration.test_pg_tenants import integration
from tests.test_api_security import HARBOR_MAPPING, MAPPING
from tests.test_tenant_isolation import SECRETS as CONNECTION_SECRETS

WEBHOOK_SECRETS = {
    "IL_MERIDIAN__WEBHOOK_SECRET": "whsec_meridian_test",
    "IL_HARBOR__WEBHOOK_SECRET": "whsec_harbor_test",
}
ALL_SECRETS = {**CONNECTION_SECRETS, **WEBHOOK_SECRETS}


def seed_tenants(engine, *, meridian_urls: tuple[str, str] | None = None) -> PostgresTenantRepository:
    """meridian + harbor with integrations (incl. webhook secret refs) and mappings."""
    m_over = {}
    if meridian_urls:
        crm, cms = meridian_urls
        m_over = dict(crm_base_url=crm, crm_token_url=f"{crm}/oauth/token", cms_base_url=cms,
                      crm_client_id="meridian-client")
    seed_postgres(engine, Tenant("meridian", "Meridian", "active"),
                  integration("meridian", webhook_secret_ref="env:IL_MERIDIAN__WEBHOOK_SECRET", **m_over), MAPPING)
    seed_postgres(engine, Tenant("harbor", "Harbor", "active"),
                  integration("harbor", webhook_secret_ref="env:IL_HARBOR__WEBHOOK_SECRET"), HARBOR_MAPPING)
    return PostgresTenantRepository(engine)


def ingestor(engine, tenants=None, secrets=None, **settings) -> WebhookIngestor:
    return WebhookIngestor(tenants=tenants or PostgresTenantRepository(engine),
                           events=PostgresWebhookEventRepository(engine),
                           secrets=EnvSecretResolver(secrets or ALL_SECRETS, app_env="test"),
                           settings=WebhookSettings(**settings))


def event_body(account="SF-1001", event_id=None, event_type="account.updated", **extra) -> bytes:
    payload = {"event_id": event_id or f"evt_{uuid.uuid4().hex}", "event_type": event_type,
               "occurred_at": "2026-11-12T10:00:00+00:00", "data": {"account_id": account}, **extra}
    return json.dumps(payload).encode()


def signed(body: bytes, secret="whsec_meridian_test", ts: int | None = None) -> dict[str, str]:
    return {"Content-Type": "application/json",
            "X-IntegrationLab-Signature": sign_header(secret, body, ts if ts is not None else int(time.time()))}


def build_service(engine, http_client_factory=None, secrets=None) -> AccountSyncService:
    repo = PostgresTenantRepository(engine)
    return AccountSyncService(
        connections=TenantConnectionProvider(repo, EnvSecretResolver(secrets or ALL_SECRETS, app_env="test"),
                                             crm_defaults=crm_settings(), cms_defaults=cms_settings(),
                                             http_client_factory=http_client_factory),
        mappings=DbMappingProvider(repo),
        history=__import__("app.repositories.sync_history", fromlist=["x"]).PostgresSyncHistoryRepository(engine),
        locks=PostgresAdvisorySyncLocks(engine, lock_timeout_seconds=30))


def build_worker(engine, service, *, lease=30.0, max_attempts=5, batch=5, worker_id=None) -> EventWorker:
    return EventWorker(events=PostgresWebhookEventRepository(engine), service=service,
                       tenants=PostgresTenantRepository(engine), lease_seconds=lease, batch_size=batch,
                       max_attempts=max_attempts, poll_interval=0.05, worker_id=worker_id)


def events(engine, tenant=None) -> list[dict]:
    sql = "SELECT * FROM webhook_events" + (" WHERE tenant_id = :t" if tenant else "") + " ORDER BY received_at"
    with engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(text(sql), {"t": tenant})]


def ingest_direct(engine, n: int, account="SF-1001", tenant="meridian", secret="whsec_meridian_test") -> list[str]:
    ing = ingestor(engine)
    ids = []
    for _ in range(n):
        body = event_body(account)
        ids.append(str(ing.ingest(tenant, signed(body, secret)["X-IntegrationLab-Signature"], body).result.id))
    return ids
