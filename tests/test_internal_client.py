"""Contract tests: InternalSystemClient against the mock internal CMS."""

from __future__ import annotations

import httpx
import pytest

from app.integrations.internal_client import (
    InternalAuthError,
    InternalConflictError,
    InternalSystemClient,
    InternalTimeoutError,
    InternalUnavailableError,
    InternalValidationError,
)
from tests.conftest import cms_settings

NEW = {"external_crm_id": "SF-1", "legal_name": "Acme", "relationship_manager": "Pat"}


def test_create_then_lookup(cms_client):
    created = cms_client.create_customer(NEW, idempotency_key="k1")
    assert created["onboarding_status"] == "pending"
    assert created["risk_classification"] == "unassessed"
    assert cms_client.get_customer_by_external_id("SF-1")["customer_id"] == created["customer_id"]


def test_lookup_missing_returns_none(cms_client):
    assert cms_client.get_customer_by_external_id("nope") is None


def test_idempotency_key_replay_returns_original(cms_app, cms_client):
    a = cms_client.create_customer(NEW, idempotency_key="same")
    b = cms_client.create_customer(NEW, idempotency_key="same")
    assert a["customer_id"] == b["customer_id"]
    assert len(cms_app.app.state.cms.customers) == 1


def test_duplicate_external_id_without_key_conflicts(cms_client):
    first = cms_client.create_customer(NEW, idempotency_key="a")
    with pytest.raises(InternalConflictError) as exc_info:
        cms_client.create_customer(NEW, idempotency_key="b")
    assert exc_info.value.existing_customer_id == first["customer_id"]


def test_wrong_api_key(cms_app):
    client = InternalSystemClient(cms_settings(api_key="wrong"), http_client=cms_app)
    with pytest.raises(InternalAuthError):
        client.get_customer_by_external_id("SF-1")


def test_rejected_payload_is_permanent(cms_client, sleeps):
    created = cms_client.create_customer(NEW, idempotency_key="k")
    with pytest.raises(InternalValidationError):
        cms_client.update_customer(created["customer_id"], {"legal_name": None})
    assert sleeps == []


def test_outage_retries_are_bounded(cms_app, sleeps):
    cms_app.put("/_admin/faults", json={"mode": "error", "remaining": 100, "status_code": 503})
    client = InternalSystemClient(cms_settings(max_retries=2), http_client=cms_app, sleep=sleeps.append)
    with pytest.raises(InternalUnavailableError):
        client.get_customer_by_external_id("SF-1")
    assert len(sleeps) == 2
    assert cms_app.get("/_admin/faults").json()["remaining"] == 97


class _LoseFirstPostResponse:
    """Forwards to the real app, but drops the response to the first POST
    (as if the network failed *after* the destination committed)."""

    def __init__(self, inner):
        self._inner = inner
        self.dropped = False

    def request(self, method, url, **kw):
        response = self._inner.request(method, url, **kw)
        if method == "POST" and not self.dropped:
            self.dropped = True
            raise httpx.ReadTimeout("response lost")
        return response

    def close(self):
        pass


def test_post_retry_after_lost_response_does_not_duplicate(cms_app):
    transport = _LoseFirstPostResponse(cms_app)
    client = InternalSystemClient(cms_settings(), http_client=transport, sleep=lambda _s: None)
    created = client.create_customer(NEW, idempotency_key="tenant:SF-1:abc")
    assert transport.dropped
    assert len(cms_app.app.state.cms.customers) == 1
    assert created["customer_id"] in cms_app.app.state.cms.customers


def test_timeout_error_type(cms_app):
    class Timeout:
        def request(self, *a, **k):
            raise httpx.ConnectTimeout("slow")

    client = InternalSystemClient(cms_settings(max_retries=0), http_client=Timeout())
    with pytest.raises(InternalTimeoutError):
        client.get_customer_by_external_id("SF-1")
