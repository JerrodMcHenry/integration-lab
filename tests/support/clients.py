"""Settings and request helpers for the in-process mock CRM / CMS."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.config import CRMSettings, InternalSystemSettings
from mock_crm import main as mock_crm
from tests.support.data import CMS_BASE, CMS_KEY, CRM_BASE


class FakeClock:
    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def crm_settings(**overrides) -> CRMSettings:
    values = dict(
        base_url=CRM_BASE, token_url=f"{CRM_BASE}/oauth/token",
        client_id=mock_crm.CLIENT_ID, client_secret=mock_crm.CLIENT_SECRET,
        timeout_seconds=2.0, max_retries=2, backoff_base_seconds=0.0, backoff_max_seconds=0.0,
    )
    values.update(overrides)
    return CRMSettings(**values)


def cms_settings(**overrides) -> InternalSystemSettings:
    values = dict(base_url=CMS_BASE, api_key=CMS_KEY, timeout_seconds=2.0, max_retries=2,
                  backoff_base_seconds=0.0, backoff_max_seconds=0.0)
    values.update(overrides)
    return InternalSystemSettings(**values)


def crm_token(crm_app: TestClient) -> str:
    r = crm_app.post("/oauth/token", data={"grant_type": "client_credentials"},
                     auth=(mock_crm.CLIENT_ID, mock_crm.CLIENT_SECRET))
    return r.json()["access_token"]


def crm_patch(crm_app: TestClient, account_id: str, **fields) -> dict:
    r = crm_app.patch(f"/crm/accounts/{account_id}", json=fields,
                      headers={"Authorization": f"Bearer {crm_token(crm_app)}"})
    assert r.status_code == 200, r.text
    return r.json()


def cms_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {CMS_KEY}"}
