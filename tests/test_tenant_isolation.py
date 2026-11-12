"""Per-tenant credentials, connections and mappings in the sync path."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.mapping.mapper import MappingConfigError
from app.repositories.sync_history import InMemorySyncHistoryRepository
from app.repositories.tenants import TenantIntegration
from app.schemas.sync import SyncStatus
from app.services.account_sync import AccountSyncService
from app.services.secrets import EnvSecretResolver, SecretResolutionError, secret_env_name
from app.services.tenant_context import DbMappingProvider, TenantConnectionError, TenantConnectionProvider
from mock_crm import main as mock_crm
from mock_internal import main as mock_internal
from tests.auth_helpers import test_tenants
from tests.conftest import cms_settings, crm_settings
from tests.test_api_security import HARBOR_MAPPING, MAPPING

SECRETS = {
    "IL_MERIDIAN__CRM_CLIENT_SECRET": "meridian-crm-secret",
    "IL_MERIDIAN__CMS_API_KEY": "meridian-cms-key",
    "IL_HARBOR__CRM_CLIENT_SECRET": "harbor-crm-secret",
    "IL_HARBOR__CMS_API_KEY": "harbor-cms-key",
}


class World:
    """Two tenants, each with its own CRM org and CMS instance (in-process)."""

    def __init__(self):
        self.crm = {
            "meridian": mock_crm.create_app(app_env="test", dataset="meridian", client_id="meridian-client",
                                            client_secret=SECRETS["IL_MERIDIAN__CRM_CLIENT_SECRET"]),
            "harbor": mock_crm.create_app(app_env="test", dataset="harbor", client_id="harbor-client",
                                          client_secret=SECRETS["IL_HARBOR__CRM_CLIENT_SECRET"]),
        }
        self.cms = {
            "meridian": mock_internal.create_app(app_env="test", api_key=SECRETS["IL_MERIDIAN__CMS_API_KEY"]),
            "harbor": mock_internal.create_app(app_env="test", api_key=SECRETS["IL_HARBOR__CMS_API_KEY"]),
        }
        self.clients: dict[str, TestClient] = {}
        self.repo = test_tenants()
        self.repo.add_mapping_version("meridian", MAPPING, "seed")
        self.repo.add_mapping_version("harbor", HARBOR_MAPPING, "seed")
        self.secrets = dict(SECRETS)
        self.provider = TenantConnectionProvider(
            self.repo, EnvSecretResolver(self.secrets, app_env="test"),
            crm_defaults=crm_settings(), cms_defaults=cms_settings(), http_client_factory=self.route)
        self.history = InMemorySyncHistoryRepository()
        self.service = AccountSyncService(connections=self.provider, mappings=DbMappingProvider(self.repo),
                                          history=self.history)

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


@pytest.fixture
def world():
    return World()


def test_each_tenant_syncs_from_its_own_crm_into_its_own_cms(world):
    m = world.service.sync_account("meridian", "SF-1001")
    h = world.service.sync_account("harbor", "HB-2001")
    assert m.status is SyncStatus.CREATED and h.status is SyncStatus.CREATED
    assert [c["external_crm_id"] for c in world.cms_state("meridian").customers.values()] == ["SF-1001"]
    assert [c["external_crm_id"] for c in world.cms_state("harbor").customers.values()] == ["HB-2001"]


def test_customer_specific_mapping_applied_per_tenant(world):
    world.service.sync_account("harbor", "HB-2001")
    world.service.sync_account("meridian", "SF-1002")
    harbor = next(iter(world.cms_state("harbor").customers.values()))
    meridian = next(iter(world.cms_state("meridian").customers.values()))
    assert harbor["legal_name"] == "TIDEWATER PENSION FUND"          # harbor: upper-case names
    assert harbor["industry_segment"] == "PENSION FUND"
    assert meridian["industry_segment"] == "ASSET_MANAGEMENT"         # meridian: value map


def test_tenant_cannot_reach_another_tenants_crm_records(world):
    r = world.service.sync_account("harbor", "SF-1001")  # a Meridian account id
    assert r.status is SyncStatus.FAILED and r.error.code == "crm_not_found"
    assert world.crm_state("meridian").account_reads == 0  # never touched Meridian's CRM
    assert world.cms_state("meridian").customers == {}


def test_crm_tokens_are_never_shared_between_tenants(world):
    world.service.sync_account("meridian", "SF-1001")
    world.service.sync_account("harbor", "HB-2001")
    m_tokens, h_tokens = set(world.crm_state("meridian").tokens), set(world.crm_state("harbor").tokens)
    assert len(m_tokens) == 1 and len(h_tokens) == 1 and m_tokens.isdisjoint(h_tokens)
    assert world.provider.for_tenant("meridian").crm is not world.provider.for_tenant("harbor").crm


def test_integration_row_pointing_at_other_tenants_secret_is_refused(world):
    i = world.repo.get_integration("meridian")
    world.repo.put_integration(TenantIntegration(**{**i.__dict__,
                                                    "crm_client_secret_ref": "env:IL_HARBOR__CRM_CLIENT_SECRET"}))
    r = world.service.sync_account("meridian", "SF-1001")
    assert r.status is SyncStatus.FAILED and r.error.code == "tenant_connection_invalid"
    assert "HARBOR" not in r.error.message and "harbor" not in r.error.message
    assert world.crm_state("harbor").tokens == {}  # harbor's credentials were never used


def test_tenant_without_integration_fails_cleanly(world):
    from app.repositories.tenants import Tenant
    world.repo.put_tenant(Tenant("newco", "NewCo", "active"))
    world.repo.add_mapping_version("newco", {**MAPPING, "tenant_id": "newco"}, "seed")
    r = world.service.sync_account("newco", "SF-1001")
    assert r.error.code == "tenant_connection_invalid" and not r.error.retryable


def test_secret_rotation_rebuilds_clients(world):
    first = world.provider.for_tenant("meridian").crm
    assert world.provider.for_tenant("meridian").crm is first  # cached
    world.secrets["IL_MERIDIAN__CRM_CLIENT_SECRET"] = "rotated"
    assert world.provider.for_tenant("meridian").crm is not first


def test_history_isolated_by_tenant(world):
    world.service.sync_account("meridian", "SF-1001")
    world.service.sync_account("harbor", "HB-2001")
    assert {r.crm_account_id for r in world.history.list_recent("meridian")} == {"SF-1001"}
    assert {r.crm_account_id for r in world.history.list_recent("harbor")} == {"HB-2001"}


def test_stored_mapping_for_wrong_tenant_is_rejected():
    repo = test_tenants()
    repo._mappings["harbor"] = [type("V", (), {"version": 1, "config": MAPPING})()]  # corrupted row
    with pytest.raises(MappingConfigError, match="different tenant"):
        DbMappingProvider(repo).get("harbor")


def test_new_mapping_version_takes_effect_without_restart(world):
    world.service.sync_account("meridian", "SF-1001")
    upper = {**MAPPING, "fields": [
        {**f, "transforms": f.get("transforms", []) + ["upper"]} if f["target"] == "legal_name" else f
        for f in MAPPING["fields"]]}
    world.repo.add_mapping_version("meridian", upper, "admin")
    r = world.service.sync_account("meridian", "SF-1001")
    assert r.mapping_version == 2 and r.fields_changed == ["legal_name"]


# --- secret resolver ----------------------------------------------------------- #
@pytest.mark.parametrize("ref", ["IL_MERIDIAN__X", "env:il_meridian__x", "vault:IL_MERIDIAN__X",
                                 "env:MERIDIAN__X", "env:IL_MERIDIAN_X", "env:IL_MERIDIAN__",
                                 "env:IL_MERIDIAN___X", "env:IL_MERIDIAN__X_", "env:IL_MERIDIAN__X__Y",
                                 "env:IL_MERIDIAN__X; rm -rf /", "", "env:"])
def test_resolver_rejects_malformed_refs(ref):
    env = {"IL_MERIDIAN__X": "v", "IL_MERIDIAN___X": "v", "IL_MERIDIAN__X_": "v", "IL_MERIDIAN__X__Y": "v"}
    with pytest.raises(SecretResolutionError):
        EnvSecretResolver(env).resolve("meridian", ref)


def test_resolver_enforces_tenant_namespace():
    r = EnvSecretResolver({"IL_HARBOR__KEY": "h", "IL_MERIDIAN__KEY": "m"})
    assert r.resolve("meridian", "env:IL_MERIDIAN__KEY") == "m"
    with pytest.raises(SecretResolutionError, match="namespace"):
        r.resolve("meridian", "env:IL_HARBOR__KEY")


@pytest.mark.parametrize("attacker, victim, victim_name", [
    ("a", "a-b", "KEY"),           # the original prefix-collision bug
    ("a", "a-b-c", "KEY"),
    ("acme", "acme-eu", "CRM_CLIENT_SECRET"),
    ("a-b", "a", "B_KEY"),         # reverse direction
])
def test_resolver_prefix_collision_between_tenant_ids_is_impossible(attacker, victim, victim_name):
    """Regression test: a prefix check let tenant 'a' read tenant 'a-b''s secrets."""
    victim_env = secret_env_name(victim, victim_name)
    r = EnvSecretResolver({victim_env: "victim-secret"})
    assert r.resolve(victim, f"env:{victim_env}") == "victim-secret"
    # Try every way the attacker could spell a reference to the victim's variable.
    with pytest.raises(SecretResolutionError):
        r.resolve(attacker, f"env:{victim_env}")


def test_tenant_ids_with_ambiguous_segments_fail_closed():
    # 'a--b' -> segment 'A__B' contains the delimiter; such a tenant can resolve nothing.
    name = secret_env_name("a--b", "KEY")
    with pytest.raises(SecretResolutionError):
        EnvSecretResolver({name: "x"}).resolve("a--b", f"env:{name}")


def test_resolver_missing_and_dev_values_in_production():
    with pytest.raises(SecretResolutionError, match="not configured"):
        EnvSecretResolver({}).resolve("meridian", "env:IL_MERIDIAN__KEY")
    with pytest.raises(SecretResolutionError, match="development value"):
        EnvSecretResolver({"IL_MERIDIAN__KEY": "dev-only-x"}, app_env="production").resolve(
            "meridian", "env:IL_MERIDIAN__KEY")


def test_connection_error_type():
    assert TenantConnectionError.code == "tenant_connection_invalid"
