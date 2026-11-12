"""HTTP API for synchronization, with the service wired to in-process mocks."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_sync_history, get_sync_service
from app.main import app


@pytest.fixture
def api(sync_service, history, auth):
    app.dependency_overrides[get_sync_service] = lambda: sync_service
    app.dependency_overrides[get_sync_history] = lambda: history
    # Meridian operator: has sync:run and history:read.
    with TestClient(app, headers=auth.headers("operator", "meridian")) as tc:
        yield tc
    app.dependency_overrides.pop(get_sync_service, None)
    app.dependency_overrides.pop(get_sync_history, None)


def test_sync_create_then_unchanged(api):
    r1 = api.post("/sync/accounts/SF-1001")
    r2 = api.post("/sync/accounts/SF-1001")
    assert r1.status_code == 201 and r1.json()["status"] == "created"
    assert r2.status_code == 200 and r2.json()["status"] == "unchanged"


def test_sync_not_found_returns_404_with_result(api):
    r = api.post("/sync/accounts/SF-404")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "crm_not_found"


def test_sync_mapping_failure_returns_422(api, crm_app):
    crm_app.put("/_admin/accounts/SF-BAD", json={"Name": ""})
    r = api.post("/sync/accounts/SF-BAD")
    assert r.status_code == 422
    assert r.json()["stage"] == "transform"


def test_sync_destination_outage_returns_502(api, cms_app):
    cms_app.put("/_admin/faults", json={"mode": "error", "remaining": 100})
    r = api.post("/sync/accounts/SF-1001")
    assert r.status_code == 502
    body = r.json()
    assert body["status"] == "failed" and body["error"]["retryable"] is True


def test_invalid_account_id_rejected(api):
    assert api.post("/sync/accounts/bad%20id").status_code == 422


def test_history_endpoints(api):
    created = api.post("/sync/accounts/SF-1001").json()
    api.post("/sync/accounts/SF-404")

    all_records = api.get("/sync/history").json()
    failed = api.get("/sync/history", params={"status": "failed"}).json()

    assert [r["crm_account_id"] for r in all_records] == ["SF-404", "SF-1001"]
    assert [r["crm_account_id"] for r in failed] == ["SF-404"]
    assert api.get(f"/sync/history/{created['id']}").json()["status"] == "created"


def test_history_record_from_other_tenant_is_invisible(api, sync_service):
    other = sync_service.sync_account("other-tenant", "SF-1001")
    assert api.get(f"/sync/history/{other.id}").status_code == 404


def test_tenant_cannot_be_chosen_by_caller(api):
    """The tenant comes only from the verified token, never from headers or parameters."""
    api.post("/sync/accounts/SF-1001", headers={"X-Tenant-ID": "harbor"}, params={"tenant_id": "harbor"})
    records = api.get("/sync/history", params={"tenant_id": "harbor"}).json()
    assert {r["tenant_id"] for r in records} == {"meridian"}


def test_openapi_documents_sync_endpoints(api):
    paths = api.get("/openapi.json").json()["paths"]
    assert "/sync/accounts/{crm_account_id}" in paths
    assert "/sync/history" in paths
