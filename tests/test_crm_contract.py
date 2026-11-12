"""Contract tests: the real CRMClient against the mock CRM app, in-process."""

from __future__ import annotations

import time

import httpx
import pytest

from app.integrations.crm_client import (
    CRMAuthError,
    CRMClient,
    CRMNotFoundError,
    CRMTimeoutError,
    CRMUnavailableError,
)
from mock_crm import main as mock_crm
from tests.support.clients import crm_patch, crm_settings, crm_token
from tests.support.servers import LiveServer


def test_fetch_account_end_to_end(crm_client):
    account = crm_client.get_account("SF-1001")
    assert account["Name"] == "Acme Capital"
    assert "LastModifiedDate" in account


def test_mock_requires_bearer_token(crm_app):
    assert crm_app.get("/crm/accounts/SF-1001").status_code == 401


def test_wrong_client_secret_rejected(crm_app):
    with pytest.raises(CRMAuthError):
        CRMClient(crm_settings(client_secret="wrong"), http_client=crm_app).get_account("SF-1001")


def test_unsupported_grant_type_rejected(crm_app):
    r = crm_app.post("/oauth/token", data={"grant_type": "password"},
                     auth=(mock_crm.CLIENT_ID, mock_crm.CLIENT_SECRET))
    assert r.status_code == 400


def test_missing_account(crm_client):
    with pytest.raises(CRMNotFoundError):
        crm_client.get_account("SF-9999")


def test_update_then_fetch_reflects_change(crm_app, crm_client):
    crm_patch(crm_app, "SF-1002", AnnualRevenue=9000000)
    account = crm_client.get_account("SF-1002")
    assert account["AnnualRevenue"] == 9000000
    assert account["LastModifiedDate"] != "2026-11-01T12:00:00+00:00"


# --- OAuth token expiration (enforced server-side) ------------------------- #
def test_mock_rejects_expired_token(crm_app, clock):
    token = crm_token(crm_app)
    headers = {"Authorization": f"Bearer {token}"}
    assert crm_app.get("/crm/accounts/SF-1001", headers=headers).status_code == 200

    clock.advance(mock_crm.TOKEN_TTL_SECONDS - 1)
    assert crm_app.get("/crm/accounts/SF-1001", headers=headers).status_code == 200

    clock.advance(1)  # exactly at expiry -> rejected
    r = crm_app.get("/crm/accounts/SF-1001", headers=headers)
    assert r.status_code == 401
    assert 'error="invalid_token"' in r.headers["www-authenticate"]


def test_client_recovers_when_server_expires_token(crm_app, clock):
    """The client's cached token is still 'valid' locally, but the server has expired it."""
    issued: list[int] = []
    original_post = crm_app.post

    def counting_post(*a, **kw):
        issued.append(1)
        return original_post(*a, **kw)

    crm_app.post = counting_post  # type: ignore[method-assign]
    client = CRMClient(crm_settings(), http_client=crm_app)
    client.get_account("SF-1001")
    clock.advance(mock_crm.TOKEN_TTL_SECONDS + 1)  # server clock only; client cache unaware
    assert client.get_account("SF-1001")["Id"] == "SF-1001"
    assert len(issued) == 2  # exactly one refresh


def test_expired_tokens_are_purged(crm_app, clock):
    for _ in range(5):
        crm_token(crm_app)
    clock.advance(mock_crm.TOKEN_TTL_SECONDS + 1)
    crm_token(crm_app)
    assert len(crm_app.app.state.crm.tokens) == 1


# --- Bounded retries -------------------------------------------------------- #
@pytest.mark.parametrize("max_retries", [0, 1, 2, 5])
def test_retry_attempts_are_exactly_bounded(crm_app, max_retries):
    crm_app.put("/_admin/faults", json={"mode": "error", "remaining": 100, "status_code": 503})
    client = CRMClient(crm_settings(max_retries=max_retries), http_client=crm_app, sleep=lambda _s: None)
    with pytest.raises(CRMUnavailableError):
        client.get_account("SF-1001")
    assert crm_app.app.state.crm.account_reads == 1 + max_retries


def test_injected_fault_recovers_within_retry_budget(crm_app, crm_client):
    crm_app.put("/_admin/faults", json={"mode": "error", "remaining": 2, "status_code": 503})
    assert crm_client.get_account("SF-1001")["Id"] == "SF-1001"


def test_injected_fault_exceeding_retry_budget_surfaces_error(crm_app):
    crm_app.put("/_admin/faults", json={"mode": "error", "remaining": 5, "status_code": 502})
    client = CRMClient(crm_settings(max_retries=1), http_client=crm_app, sleep=lambda _s: None)
    with pytest.raises(CRMUnavailableError) as exc_info:
        client.get_account("SF-1001")
    assert exc_info.value.status_code == 502
    assert crm_app.get("/_admin/faults").json()["remaining"] == 3


def test_injected_timeout_against_live_server():
    """Real socket timeout: run the mock CRM under uvicorn on an ephemeral port."""
    with LiveServer(mock_crm.create_app(app_env="test")) as server:
        base = server.url
        httpx.put(f"{base}/_admin/faults", json={"mode": "timeout", "remaining": 1, "delay_seconds": 2})
        client = CRMClient(crm_settings(base_url=base, token_url=f"{base}/oauth/token",
                                        timeout_seconds=0.3, max_retries=0))
        started = time.monotonic()
        with pytest.raises(CRMTimeoutError):
            client.get_account("SF-1001")
        assert time.monotonic() - started < 1.5
        client.close()
