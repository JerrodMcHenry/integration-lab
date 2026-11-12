"""Per-tenant integration clients and mappings.

``TenantConnectionProvider`` builds a separate ``CRMClient`` and
``InternalSystemClient`` for each tenant, from that tenant's own integration
row and secrets. Each CRM client has its own OAuth token cache, so a token
issued for one tenant's CRM org is never presented on behalf of another
tenant.

``DbMappingProvider`` loads the tenant's active mapping version and caches
the compiled mapper by (tenant, version). Publishing a new version takes
effect on the next sync, with no restart.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.core.config import CRMSettings, HttpPolicy, InternalSystemSettings
from app.integrations.crm_client import CRMClient
from app.integrations.internal_client import InternalSystemClient
from app.mapping.mapper import FieldMapper, MappingConfigError
from app.repositories.tenants import TenantIntegration, TenantRepository
from app.services.secrets import EnvSecretResolver, SecretResolutionError


class TenantConnectionError(RuntimeError):
    code = "tenant_connection_invalid"
    retryable = False


@dataclass(frozen=True)
class TenantClients:
    crm: CRMClient
    destination: InternalSystemClient


class ConnectionProvider(Protocol):
    def for_tenant(self, tenant_id: str) -> TenantClients: ...


class TenantConnectionProvider:
    def __init__(self, repo: TenantRepository, secrets: EnvSecretResolver, *,
                 crm_defaults: HttpPolicy, cms_defaults: HttpPolicy,
                 require_https: bool = False,
                 max_cached: int = 256,
                 http_client_factory: Callable[[str], httpx.Client] | None = None) -> None:
        self._repo = repo
        self._http_factory = http_client_factory  # tests route base URLs to in-process mocks
        self._secrets = secrets
        #: Production: tenant CRM/CMS endpoints must be HTTPS (replaces the former
        #: global CRM_BASE_URL / INTERNAL_API_BASE_URL https checks).
        self._require_https = require_https
        self._crm_defaults = crm_defaults
        self._cms_defaults = cms_defaults
        self._cache: OrderedDict[tuple[str, str], TenantClients] = OrderedDict()
        self._max = max_cached
        self._lock = threading.Lock()

    def for_tenant(self, tenant_id: str) -> TenantClients:
        integration = self._repo.get_integration(tenant_id)
        if integration is None or integration.tenant_id != tenant_id:
            raise TenantConnectionError("tenant has no integration configured")
        if self._require_https:
            insecure = [name for name, url in (("crm_base_url", integration.crm_base_url),
                                               ("crm_token_url", integration.crm_token_url),
                                               ("cms_base_url", integration.cms_base_url))
                        if not url.startswith("https://")]
            if insecure:
                raise TenantConnectionError(f"tenant endpoints must use https in production: {insecure}")
        try:
            crm_secret = self._secrets.resolve(tenant_id, integration.crm_client_secret_ref)
            cms_key = self._secrets.resolve(tenant_id, integration.cms_api_key_ref)
        except SecretResolutionError as exc:
            raise TenantConnectionError(str(exc)) from exc

        # Rebuild clients when the integration row or a secret changes (rotation).
        fingerprint = _fingerprint(integration, crm_secret, cms_key)
        key = (tenant_id, fingerprint)
        with self._lock:
            clients = self._cache.get(key)
            if clients is not None:
                self._cache.move_to_end(key)
                return clients
            for stale in [k for k in self._cache if k[0] == tenant_id]:
                self._close(self._cache.pop(stale))
            clients = self._build(integration, crm_secret, cms_key)
            self._cache[key] = clients
            while len(self._cache) > self._max:
                self._close(self._cache.popitem(last=False)[1])
            return clients

    def _build(self, i: TenantIntegration, crm_secret: str, cms_key: str) -> TenantClients:
        d, c = self._crm_defaults, self._cms_defaults
        crm = CRMClient(CRMSettings(
            base_url=i.crm_base_url.rstrip("/"), token_url=i.crm_token_url, client_id=i.crm_client_id,
            client_secret=crm_secret, timeout_seconds=d.timeout_seconds, max_retries=d.max_retries,
            backoff_base_seconds=d.backoff_base_seconds, backoff_max_seconds=d.backoff_max_seconds),
            http_client=self._http_factory(i.crm_base_url) if self._http_factory else None)
        cms = InternalSystemClient(InternalSystemSettings(
            base_url=i.cms_base_url.rstrip("/"), api_key=cms_key, timeout_seconds=c.timeout_seconds,
            max_retries=c.max_retries, backoff_base_seconds=c.backoff_base_seconds,
            backoff_max_seconds=c.backoff_max_seconds),
            http_client=self._http_factory(i.cms_base_url) if self._http_factory else None)
        return TenantClients(crm, cms)

    @staticmethod
    def _close(clients: TenantClients) -> None:
        clients.crm.close()
        clients.destination.close()


def _fingerprint(i: TenantIntegration, *secrets: str) -> str:
    h = hashlib.sha256()
    for part in (i.crm_base_url, i.crm_token_url, i.crm_client_id, i.cms_base_url, *secrets):
        h.update(part.encode())
        h.update(b"\x00")
    return h.hexdigest()


class DbMappingProvider:
    def __init__(self, repo: TenantRepository, max_cached: int = 256) -> None:
        self._repo = repo
        self._cache: OrderedDict[tuple[str, int], FieldMapper] = OrderedDict()
        self._max = max_cached
        self._lock = threading.Lock()

    def get(self, tenant_id: str) -> FieldMapper:
        active = self._repo.get_active_mapping(tenant_id)
        if active is None:
            raise MappingConfigError([f"no mapping configured for tenant '{tenant_id}'"])
        key = (tenant_id, active.version)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        mapper = FieldMapper.from_dict(active.config)
        if mapper.tenant_id != tenant_id:
            raise MappingConfigError(["stored mapping belongs to a different tenant"])
        with self._lock:
            self._cache[key] = mapper
            while len(self._cache) > self._max:
                self._cache.popitem(last=False)
        return mapper
