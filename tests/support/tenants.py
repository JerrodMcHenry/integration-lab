"""Tenant fixtures-as-code: an in-memory tenant directory, integration rows, and a two-tenant world."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.support.fakes import InMemorySyncHistoryRepository
from app.repositories.tenants import Tenant, TenantIntegration
from tests.support.fakes import InMemoryTenantRepository
from app.services.account_sync import AccountSyncService
from app.services.secrets import EnvSecretResolver
from app.services.tenant_context import DbMappingProvider, TenantConnectionProvider
from mock_crm import main as mock_crm
from mock_internal import main as mock_internal
from tests.support.clients import cms_settings, crm_settings
from tests.support.data import CONNECTION_SECRETS, HARBOR_MAPPING, MAPPING
from tests.support.fakes import InProcessSyncLocks


def in_memory_tenants() -> InMemoryTenantRepository:
    """meridian + harbor (active), suspended-co (suspended)."""
    repo = InMemoryTenantRepository()
    repo.put_tenant(Tenant("meridian", "Meridian Capital", "active"))
    repo.put_tenant(Tenant("harbor", "Harborview Trust", "active"))
    repo.put_tenant(Tenant("suspended-co", "Suspended Co", "suspended"))
    for t in ("meridian", "harbor"):
        up = t.upper()
        repo.put_integration(TenantIntegration(
            tenant_id=t, crm_base_url=f"http://crm-{t}.test", crm_token_url=f"http://crm-{t}.test/oauth/token",
            crm_client_id=f"{t}-client", crm_client_secret_ref=f"env:IL_{up}__CRM_CLIENT_SECRET",
            cms_base_url=f"http://cms-{t}.test", cms_api_key_ref=f"env:IL_{up}__CMS_API_KEY",
            webhook_secret_ref=f"env:IL_{up}__WEBHOOK_SECRET"))
    return repo


def integration(tenant: str, **overrides) -> TenantIntegration:
    up = tenant.upper()
    values = dict(tenant_id=tenant, crm_base_url=f"http://crm-{tenant}.test",
                  crm_token_url=f"http://crm-{tenant}.test/oauth/token", crm_client_id=f"{tenant}-client",
                  crm_client_secret_ref=f"env:IL_{up}__CRM_CLIENT_SECRET", cms_base_url=f"http://cms-{tenant}.test",
                  cms_api_key_ref=f"env:IL_{up}__CMS_API_KEY")
    values.update(overrides)
    return TenantIntegration(**values)


class World:
    """Two tenants, each with its own CRM org and CMS instance (in-process)."""

    def __init__(self):
        self.crm = {
            "meridian": mock_crm.create_app(app_env="test", dataset="meridian", client_id="meridian-client",
                                            client_secret=CONNECTION_SECRETS["IL_MERIDIAN__CRM_CLIENT_SECRET"]),
            "harbor": mock_crm.create_app(app_env="test", dataset="harbor", client_id="harbor-client",
                                          client_secret=CONNECTION_SECRETS["IL_HARBOR__CRM_CLIENT_SECRET"]),
        }
        self.cms = {
            "meridian": mock_internal.create_app(app_env="test", api_key=CONNECTION_SECRETS["IL_MERIDIAN__CMS_API_KEY"]),
            "harbor": mock_internal.create_app(app_env="test", api_key=CONNECTION_SECRETS["IL_HARBOR__CMS_API_KEY"]),
        }
        self.clients: dict[str, TestClient] = {}
        self.repo = in_memory_tenants()
        self.repo.add_mapping_version("meridian", MAPPING, "seed")
        self.repo.add_mapping_version("harbor", HARBOR_MAPPING, "seed")
        self.secrets = dict(CONNECTION_SECRETS)
        self.provider = TenantConnectionProvider(
            self.repo, EnvSecretResolver(self.secrets, app_env="test"),
            crm_defaults=crm_settings(), cms_defaults=cms_settings(), http_client_factory=self.route)
        self.history = InMemorySyncHistoryRepository()
        self.service = AccountSyncService(connections=self.provider, mappings=DbMappingProvider(self.repo),
                                          history=self.history, locks=InProcessSyncLocks())

    def route(self, base_url: str) -> TestClient:
        # http://crm-meridian.test -> the meridian CRM app, etc.
        host = base_url.split("//", 1)[1].split("/", 1)[0]
        kind, tenant = host.split(".")[0].split("-", 1)
        if base_url not in self.clients:
            app = (self.crm if kind == "crm" else self.cms)[tenant]
            self.clients[base_url] = TestClient(app, base_url=f"http://{host}")
        return self.clients[base_url]

    def crm_state(self, tenant):
        return self.crm[tenant].state.crm

    def cms_state(self, tenant):
        return self.cms[tenant].state.cms
