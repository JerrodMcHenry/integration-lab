"""Webhook ingestion over HTTP against real PostgreSQL."""

from __future__ import annotations

import json
import logging
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.dependencies import get_event_repository, get_webhook_ingestor
from app.main import app
from app.repositories.webhook_events import PostgresWebhookEventRepository
from mock_crm import main as mock_crm
from tests.support.data import ALL_SECRETS
from tests.support.webhooks import event_body, events, ingestor, seed_tenants, signed
from tests.support.concurrency import run_in_threads

M_SECRET, H_SECRET = "whsec_meridian_test", "whsec_harbor_test"


@pytest.fixture
def api(pg_engine, auth):
    seed_tenants(pg_engine)
    holder = {"ing": ingestor(pg_engine)}
    app.dependency_overrides[get_webhook_ingestor] = lambda: holder["ing"]
    app.dependency_overrides[get_event_repository] = lambda: PostgresWebhookEventRepository(pg_engine)
    with TestClient(app) as tc:
        tc.holder = holder
        yield tc
    app.dependency_overrides.pop(get_webhook_ingestor, None)
    app.dependency_overrides.pop(get_event_repository, None)


def post(api, body, headers, tenant="meridian"):
    return api.post(f"/webhooks/crm/{tenant}", content=body, headers=headers)


# --------------------------------------------------------------------------- #
# Accept + persist-before-ack
# --------------------------------------------------------------------------- #
def test_valid_event_is_persisted_before_acknowledgment(api, pg_engine):
    body = event_body("SF-1001", event_id="evt_a")
    r = post(api, body, signed(body))
    assert r.status_code == 202
    assert r.json()["status"] == "accepted" and r.json()["event_status"] == "pending"
    rows = events(pg_engine)  # committed: visible to a separate connection right after the response
    assert len(rows) == 1 and str(rows[0]["id"]) == r.json()["id"]
    row = rows[0]
    assert (row["tenant_id"], row["event_id"], row["crm_account_id"], row["status"], row["attempts"]) == (
        "meridian", "evt_a", "SF-1001", "pending", 0)


def test_only_allowlisted_envelope_is_stored(api, pg_engine):
    body = event_body("SF-1001", event_id="evt_x", account_name="Confidential Corp", ssn="123-45-6789")
    headers = signed(body)
    assert post(api, body, headers).status_code == 202
    row = events(pg_engine)[0]
    assert set(row["payload"]) == {"event_id", "event_type", "occurred_at", "account_id"}
    blob = json.dumps({k: str(v) for k, v in row.items()})
    for secret_or_pii in (M_SECRET, headers["X-IntegrationLab-Signature"].split("v1=")[1],
                          "Confidential Corp", "123-45-6789"):
        assert secret_or_pii not in blob


def test_secrets_and_signatures_never_logged(api, caplog):
    body = event_body()
    headers = signed(body)
    with caplog.at_level(logging.DEBUG):
        post(api, body, headers)
        post(api, body, signed(body, "wrong-secret"))
    assert M_SECRET not in caplog.text
    assert headers["X-IntegrationLab-Signature"].split("v1=")[1] not in caplog.text


# --------------------------------------------------------------------------- #
# Authentication of the sender
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("case", ["wrong_secret", "tampered", "missing", "malformed", "other_tenant_secret",
                                  "unknown_tenant", "bad_tenant_path"])
def test_unauthenticated_deliveries_rejected_identically(api, pg_engine, case):
    body = event_body()
    tenant = "meridian"
    if case == "wrong_secret":
        headers = signed(body, "nope")
    elif case == "tampered":
        headers = signed(body)
        body = body.replace(b"SF-1001", b"SF-1002")
    elif case == "missing":
        headers = {"Content-Type": "application/json"}
    elif case == "malformed":
        headers = {"X-IntegrationLab-Signature": "t=1,v1=zz"}
    elif case == "other_tenant_secret":
        headers, tenant = signed(body, M_SECRET), "harbor"  # Meridian's key can't speak for Harbor
    elif case == "unknown_tenant":
        headers, tenant = signed(body), "ghost-co"
    else:
        headers, tenant = signed(body), "..%2Fmeridian"
    r = post(api, body, headers, tenant)
    assert r.status_code in (401, 404)
    if r.status_code == 401:
        assert r.json() == {"detail": "signature verification failed"}  # no tenant-existence oracle
    assert events(pg_engine) == []


def test_tenant_without_configured_secret_fails_closed(api, pg_engine):
    api.holder["ing"] = ingestor(pg_engine, secrets={k: v for k, v in ALL_SECRETS.items()
                                                      if k != "IL_MERIDIAN__WEBHOOK_SECRET"})
    body = event_body()
    assert post(api, body, signed(body)).status_code == 401
    assert events(pg_engine) == []


def test_payload_tenant_claim_is_not_trusted(api, pg_engine):
    body = event_body(tenant_id="meridian")
    assert post(api, body, signed(body, H_SECRET), "harbor").status_code == 422  # mismatch rejected
    body2 = event_body(tenant_id="harbor")
    assert post(api, body2, signed(body2, H_SECRET), "harbor").status_code == 202
    assert [r["tenant_id"] for r in events(pg_engine)] == ["harbor"]


def test_suspended_tenant_rejected_after_authentication(api, pg_engine):
    with pg_engine.begin() as c:
        c.execute(text("UPDATE tenants SET status='suspended' WHERE id='meridian'"))
    body = event_body()
    assert post(api, body, signed(body)).status_code == 403


def test_secret_rotation_accepts_previous_secret(api, pg_engine):
    with pg_engine.begin() as c:
        c.execute(text("UPDATE tenant_integrations SET webhook_secret_previous_ref = "
                       "'env:IL_MERIDIAN__WEBHOOK_SECRET_OLD' WHERE tenant_id='meridian'"))
    api.holder["ing"] = ingestor(pg_engine, secrets={**ALL_SECRETS, "IL_MERIDIAN__WEBHOOK_SECRET_OLD": "whsec_old"})
    body = event_body()
    assert post(api, body, signed(body, "whsec_old")).status_code == 202


# --------------------------------------------------------------------------- #
# Replay protection
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("age", [301, 86_400, -400])
def test_stale_or_future_timestamps_rejected(api, pg_engine, age):
    body = event_body()
    r = post(api, body, signed(body, ts=int(time.time()) - age))
    assert r.status_code == 401 and r.json()["detail"] == "signature timestamp outside tolerance"
    assert events(pg_engine) == []


def test_tolerance_is_configurable(api, pg_engine):
    api.holder["ing"] = ingestor(pg_engine, tolerance_seconds=60)
    body = event_body()
    assert post(api, body, signed(body, ts=int(time.time()) - 120)).status_code == 401


def test_captured_request_replayed_inside_window_is_a_duplicate(api, pg_engine):
    body = event_body(event_id="evt_replay")
    headers = signed(body)
    first, replay = post(api, body, headers), post(api, body, headers)
    assert (first.status_code, replay.status_code) == (202, 200)
    assert replay.json()["status"] == "duplicate" and replay.json()["id"] == first.json()["id"]
    assert len(events(pg_engine)) == 1


# --------------------------------------------------------------------------- #
# Idempotency / duplicates
# --------------------------------------------------------------------------- #
def test_provider_retry_with_new_signature_is_duplicate(api, pg_engine):
    body = event_body(event_id="evt_retry")
    a = post(api, body, signed(body, ts=int(time.time()) - 100))
    b = post(api, body, signed(body))
    assert (a.status_code, b.status_code) == (202, 200)
    assert b.json() == {"status": "duplicate", "id": a.json()["id"], "event_id": "evt_retry",
                        "event_status": "pending"}


def test_same_event_id_different_content_is_conflict(api, pg_engine):
    a = event_body("SF-1001", event_id="evt_c")
    b = event_body("SF-1002", event_id="evt_c")
    assert post(api, a, signed(a)).status_code == 202
    r = post(api, b, signed(b))
    assert r.status_code == 409 and r.json()["status"] == "conflict"
    assert [e["crm_account_id"] for e in events(pg_engine)] == ["SF-1001"]


def test_idempotency_is_tenant_scoped(api, pg_engine):
    m = event_body(event_id="evt_same")
    h = event_body(event_id="evt_same")
    assert post(api, m, signed(m, M_SECRET), "meridian").status_code == 202
    assert post(api, h, signed(h, H_SECRET), "harbor").status_code == 202
    assert sorted(e["tenant_id"] for e in events(pg_engine)) == ["harbor", "meridian"]


def test_concurrent_duplicate_deliveries_create_one_row(api, pg_engine):
    body = event_body(event_id="evt_race")
    codes = sorted(run_in_threads(lambda: post(api, body, signed(body)).status_code,
                                  count=8, timeout=60, what="8 concurrent duplicate deliveries"))
    assert codes == [200] * 7 + [202]
    assert len(events(pg_engine)) == 1


def test_unique_constraint_enforced_in_database(pg_engine):
    seed_tenants(pg_engine)
    ins = ("INSERT INTO webhook_events (id, tenant_id, source, event_id, event_type, signed_at, payload, "
           "payload_sha256) VALUES (gen_random_uuid(), 'meridian', 'crm', 'evt_u', 'account.updated', now(), "
           "'{}', repeat('a', 64))")
    with pg_engine.begin() as c:
        c.execute(text(ins))
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError, match="uq_webhook_events_tenant_id_source_event_id"), pg_engine.begin() as c:
        c.execute(text(ins))


# --------------------------------------------------------------------------- #
# Validation, limits, availability
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw", [b"not json", b"[]", b'{"event_type":"account.updated"}',
                                 b'{"event_id":"e1","event_type":"account.updated"}',
                                 b'{"event_id":"bad id!","event_type":"account.updated","data":{"account_id":"A"}}'])
def test_invalid_payloads_rejected_after_auth(api, pg_engine, raw):
    assert post(api, raw, signed(raw)).status_code == 422
    assert events(pg_engine) == []


def test_unsupported_event_type_acknowledged_as_ignored(api, pg_engine):
    body = event_body(event_type="opportunity.closed")
    r = post(api, body, signed(body))
    assert r.status_code == 202 and r.json()["status"] == "ignored"
    assert events(pg_engine)[0]["status"] == "ignored"


def test_oversized_body_rejected(api, pg_engine):
    body = event_body(padding="x" * 70_000)
    assert post(api, body, signed(body)).status_code == 413
    assert events(pg_engine) == []


def test_database_down_returns_503_and_acknowledges_nothing(api, pg_engine, dead_engine):
    from app.repositories.tenants import PostgresTenantRepository
    # Tenant lookup works, the event store is down (worst case: authenticated, can't persist).
    api.holder["ing"] = ingestor(dead_engine, tenants=PostgresTenantRepository(pg_engine))
    body = event_body()
    r = post(api, body, signed(body))
    assert r.status_code == 503 and "retry" in r.json()["detail"]
    assert events(pg_engine) == []


def test_tenant_directory_down_returns_503(api, dead_engine):
    api.holder["ing"] = ingestor(dead_engine)
    body = event_body()
    assert post(api, body, signed(body)).status_code == 503


# --------------------------------------------------------------------------- #
# Inspection endpoints (OIDC-protected, tenant-scoped)
# --------------------------------------------------------------------------- #
def test_event_listing_is_tenant_scoped_and_protected(api, auth):
    m, h = event_body(), event_body()
    mid = post(api, m, signed(m, M_SECRET), "meridian").json()["id"]
    post(api, h, signed(h, H_SECRET), "harbor")
    assert api.get("/webhooks/events").status_code == 401
    no_role = {"Authorization": f"Bearer {auth.token(roles=[])}"}
    assert api.get("/webhooks/events", headers=no_role).status_code == 403
    mine = api.get("/webhooks/events", headers=auth.headers("viewer", "meridian")).json()
    assert [e["id"] for e in mine] == [mid]
    assert api.get(f"/webhooks/events/{mid}", headers=auth.headers("admin", "harbor")).status_code == 404
    assert api.get(f"/webhooks/events/{mid}", headers=auth.headers("viewer", "meridian")).json()["status"] == "pending"


# --------------------------------------------------------------------------- #
# Interoperability: the mock CRM's sender -> IntegrationLab
# --------------------------------------------------------------------------- #
def test_mock_crm_update_emits_webhook_that_is_accepted(api, pg_engine):
    sender = mock_crm.WebhookSender("http://testserver/webhooks/crm/meridian", M_SECRET, client=api,
                                    asynchronous=False)
    crm = TestClient(mock_crm.create_app(app_env="test", webhook_sender=sender))
    token = crm.post("/oauth/token", data={"grant_type": "client_credentials"},
                     auth=(mock_crm.CLIENT_ID, mock_crm.CLIENT_SECRET)).json()["access_token"]
    crm.patch("/crm/accounts/SF-1002", json={"AnnualRevenue": 1}, headers={"Authorization": f"Bearer {token}"})
    event_id = sender.deliveries[-1]["event_id"]
    crm.post(f"/_admin/webhooks/redeliver/{event_id}")
    assert [d["status"] for d in sender.deliveries] == [202, 200]  # accepted, then duplicate
    rows = events(pg_engine)
    assert len(rows) == 1 and rows[0]["crm_account_id"] == "SF-1002" and rows[0]["event_id"] == event_id
