"""Manual replay, attempt history API, metrics, audit log and retention (real PostgreSQL)."""

from __future__ import annotations

import dataclasses
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app import retention
from app.api import dependencies as deps
from app.core.config import RetentionSettings
from app.main import app
from app.repositories.audit import PostgresAuditRepository
from app.repositories.operations import PostgresOperationsRepository
from app.repositories.webhook_events import PostgresWebhookEventRepository
from tests.support.webhooks import attempts, crm_fault, drain, fast_policy
from tests.support.webhooks import by_id
from tests.support.webhooks import build_worker, event_body, ingest_direct, ingestor, signed
from tests.support.concurrency import run_in_threads


@pytest.fixture
def api(pg_engine, auth, world):
    audit = PostgresAuditRepository(pg_engine)
    app.dependency_overrides[deps.get_event_repository] = lambda: PostgresWebhookEventRepository(pg_engine)
    app.dependency_overrides[deps.get_operations_repository] = lambda: PostgresOperationsRepository(pg_engine)
    app.dependency_overrides[deps.get_audit_repository] = lambda: audit
    with TestClient(app) as tc:
        yield tc
    for dep in (deps.get_event_repository, deps.get_operations_repository, deps.get_audit_repository):
        app.dependency_overrides.pop(dep, None)


def audit_rows(engine, action=None):
    sql = "SELECT tenant_id, actor, action, outcome, target_id, details FROM audit_log"
    sql += " WHERE action = :a" if action else ""
    with engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(text(sql + " ORDER BY id"), {"a": action})]


def dead_letter_event(engine, world, svc, account="SF-1001", tenant="meridian"):
    secret = "whsec_meridian_test" if tenant == "meridian" else "whsec_harbor_test"
    crm_fault(world, 100, tenant)
    [eid] = ingest_direct(engine, 1, account, tenant=tenant, secret=secret)
    drain(engine, svc, policy=fast_policy(max_attempts=2), until=lambda: by_id(engine)[eid]["status"] == "dead_letter")
    world.crm[tenant].state.crm.fault = type(world.crm[tenant].state.crm.fault)()  # outage over
    return eid


# --------------------------------------------------------------------------- #
# Manual replay
# --------------------------------------------------------------------------- #
def test_operator_replays_dead_letter_and_it_succeeds(api, auth, pg_engine, world, service):
    eid = dead_letter_event(pg_engine, world, service, "SF-1002")
    r = api.post(f"/webhooks/events/{eid}/replay", json={"reason": "CRM outage resolved"},
                 headers=auth.headers("operator", "meridian", sub="ops@meridian"))
    assert r.status_code == 202 and r.json() == {"status": "replayed", "id": eid, "previous_status": "dead_letter",
                                                 "new_status": "pending"}
    ev = by_id(pg_engine)[eid]
    assert (ev["status"], ev["attempts"], ev["replay_count"], ev["last_replayed_by"], ev["dead_lettered_at"]) == (
        "pending", 0, 1, "ops@meridian", None)
    [a] = audit_rows(pg_engine, "webhook_event.replay")
    assert (a["tenant_id"], a["actor"], a["outcome"], a["target_id"]) == ("meridian", "ops@meridian", "success", eid)
    assert a["details"] == {"reason": "CRM outage resolved", "previous_status": "dead_letter"}

    build_worker(pg_engine, service).run_once()
    assert by_id(pg_engine)[eid]["status"] == "succeeded"
    hist = [h["outcome"] for h in attempts(pg_engine, eid)]
    assert hist == ["retry_scheduled", "dead_letter", "succeeded"]  # history kept across replay
    assert len(world.cms_state("meridian").customers) == 1


def test_admin_can_replay_permanent_failure(api, auth, pg_engine, world, service):
    [eid] = ingest_direct(pg_engine, 1, "SF-404")
    build_worker(pg_engine, service).run_once()
    assert by_id(pg_engine)[eid]["status"] == "failed"
    assert api.post(f"/webhooks/events/{eid}/replay", headers=auth.headers("admin")).status_code == 202


def test_replay_preserves_idempotency(api, auth, pg_engine, world, service):
    """Replaying an event whose change was already applied produces 'unchanged', never a duplicate."""
    [eid] = ingest_direct(pg_engine, 1, "SF-1002")
    build_worker(pg_engine, service).run_once()
    with pg_engine.begin() as c:  # force it into a replayable state
        c.execute(text("UPDATE webhook_events SET status='failed', last_error_code='manual' WHERE id=:e"), {"e": eid})
    api.post(f"/webhooks/events/{eid}/replay", headers=auth.headers("operator"))
    build_worker(pg_engine, service).run_once()
    assert len(world.cms_state("meridian").customers) == 1
    rec_id = by_id(pg_engine)[eid]["sync_record_id"]
    with pg_engine.connect() as c:
        assert c.execute(text("SELECT status FROM sync_records WHERE id=:r"), {"r": rec_id}).scalar() == "unchanged"


@pytest.mark.parametrize("status", ["pending", "processing", "succeeded"])
def test_non_replayable_states_conflict_and_are_audited(api, auth, pg_engine, world, service, status):
    [eid] = ingest_direct(pg_engine, 1)
    if status == "processing":
        PostgresWebhookEventRepository(pg_engine).claim("w", batch=1, lease_seconds=60, max_attempts=5)
    elif status == "succeeded":
        build_worker(pg_engine, service).run_once()
    r = api.post(f"/webhooks/events/{eid}/replay", headers=auth.headers("operator"))
    assert r.status_code == 409 and status in r.json()["detail"]
    assert by_id(pg_engine)[eid]["status"] == status
    assert [a["outcome"] for a in audit_rows(pg_engine, "webhook_event.replay")] == ["rejected"]


def test_concurrent_replays_only_one_wins(api, auth, pg_engine, world, service):
    eid = dead_letter_event(pg_engine, world, service)
    h = auth.headers("operator")
    codes = sorted(run_in_threads(lambda: api.post(f"/webhooks/events/{eid}/replay", headers=h).status_code,
                                  count=6, timeout=60, what="6 concurrent replay requests"))
    assert codes == [202] + [409] * 5
    assert by_id(pg_engine)[eid]["replay_count"] == 1


@pytest.mark.parametrize("roles", [["viewer"], []])
def test_unauthorized_replay_is_denied_and_audited(api, auth, pg_engine, world, service, roles):
    eid = dead_letter_event(pg_engine, world, service)
    r = api.post(f"/webhooks/events/{eid}/replay",
                 headers={"Authorization": f"Bearer {auth.token(roles=roles, sub='intruder')}"})
    assert r.status_code == 403
    assert by_id(pg_engine)[eid]["status"] == "dead_letter"
    [a] = audit_rows(pg_engine, "webhook_event.replay")
    assert (a["actor"], a["outcome"], a["details"]["missing_permission"]) == ("intruder", "denied", "sync:run")


def test_unauthenticated_replay_rejected(api, pg_engine, world, service):
    eid = dead_letter_event(pg_engine, world, service)
    assert api.post(f"/webhooks/events/{eid}/replay").status_code == 401
    assert api.post(f"/webhooks/events/{eid}/replay", headers={"Authorization": "Bearer x.y.z"}).status_code == 401


def test_cross_tenant_replay_is_not_found(api, auth, pg_engine, world, service):
    eid = dead_letter_event(pg_engine, world, service)
    assert api.post(f"/webhooks/events/{eid}/replay", headers=auth.headers("admin", "harbor")).status_code == 404
    assert by_id(pg_engine)[eid]["status"] == "dead_letter"
    assert api.post(f"/webhooks/events/{uuid.uuid4()}/replay", headers=auth.headers("admin")).status_code == 404


def test_bulk_replay_is_bounded_scoped_and_audited(api, auth, pg_engine, world, service):
    m = [dead_letter_event(pg_engine, world, service, a) for a in ("SF-1001", "SF-1002", "SF-1001")]
    h = dead_letter_event(pg_engine, world, service, "HB-2001", tenant="harbor")
    r = api.post("/webhooks/events/replay", json={"statuses": ["dead_letter"], "limit": 2, "reason": "outage"},
                 headers=auth.headers("operator", "meridian"))
    assert r.status_code == 200 and r.json()["replayed"] == 2 and set(r.json()["ids"]) <= set(m)
    rows = by_id(pg_engine)
    assert sorted(rows[e]["status"] for e in m) == ["dead_letter", "pending", "pending"]
    assert rows[h]["status"] == "dead_letter"  # other tenant untouched
    audits = audit_rows(pg_engine, "webhook_event.replay")
    assert len(audits) == 2 and all(a["tenant_id"] == "meridian" and a["details"]["bulk"] for a in audits)
    assert api.post("/webhooks/events/replay", json={"statuses": ["succeeded"]},
                    headers=auth.headers("operator")).status_code == 422
    assert api.post("/webhooks/events/replay", json={"limit": 1000},
                    headers=auth.headers("operator")).status_code == 422
    assert api.post("/webhooks/events/replay", json={}, headers=auth.headers("viewer")).status_code == 403


# --------------------------------------------------------------------------- #
# Attempt history API
# --------------------------------------------------------------------------- #
def test_attempts_endpoint_is_tenant_scoped(api, auth, pg_engine, world, service):
    eid = dead_letter_event(pg_engine, world, service)
    hist = api.get(f"/webhooks/events/{eid}/attempts", headers=auth.headers("viewer")).json()
    assert [h["outcome"] for h in hist] == ["retry_scheduled", "dead_letter"]
    assert hist[0]["next_attempt_at"] is not None and hist[0]["error_code"] == "crm_unavailable"
    assert api.get(f"/webhooks/events/{eid}/attempts", headers=auth.headers("admin", "harbor")).status_code == 404
    ev = api.get(f"/webhooks/events/{eid}", headers=auth.headers("viewer")).json()
    assert ev["status"] == "dead_letter" and ev["dead_lettered_at"] is not None


# --------------------------------------------------------------------------- #
# Metrics and worker health
# --------------------------------------------------------------------------- #
def test_ops_metrics_reflect_queue_state(api, auth, pg_engine, world, service):
    dead_letter_event(pg_engine, world, service)                                  # 1 dead letter
    ingest_direct(pg_engine, 1, "SF-404")
    build_worker(pg_engine, service, worker_id="m-worker").run_once()             # 1 failed (permanent)
    ingest_direct(pg_engine, 2)                                                    # 2 pending, ready
    crm_fault(world, 100)
    ingest_direct(pg_engine, 1)
    build_worker(pg_engine, service, batch=1, retry_policy=fast_policy(base=30, cap=60)).run_once()
    ingest_direct(pg_engine, 1, tenant="harbor", secret="whsec_harbor_test")      # other tenant's backlog
    m = api.get("/ops/metrics", headers=auth.headers("viewer")).json()
    e = m["events"]
    assert m["tenant_id"] == "meridian"
    assert (e["dead_letter"], e["failed"], e["retry_backlog"]) == (1, 1, 1)
    assert e["pending"] == 3 and e["ready"] == 2 and e["processing"] == 0
    assert e["oldest_pending_s"] is not None and e["oldest_pending_s"] >= 0
    assert m["workers"]["healthy"] >= 1 and m["workers"]["total_seen"] >= 1
    harbor = api.get("/ops/metrics", headers=auth.headers("viewer", "harbor")).json()["events"]
    assert (harbor["pending"], harbor["dead_letter"]) == (1, 0)


def test_latency_metrics_after_success(api, auth, pg_engine, world, service):
    ingest_direct(pg_engine, 3)
    build_worker(pg_engine, service).run(max_events=3, max_seconds=5)
    e = api.get("/ops/metrics", headers=auth.headers("viewer")).json()["events"]
    assert e["succeeded_1h"] == 3 and 0 <= e["latency_p50_s"] <= e["latency_p95_s"] <= e["latency_max_s"] < 30


def test_ops_metrics_requires_auth(api, auth):
    assert api.get("/ops/metrics").status_code == 401
    assert api.get("/ops/metrics", headers={"Authorization": f"Bearer {auth.token(roles=[])}"}).status_code == 403


def test_prometheus_endpoint_requires_its_own_token(api, auth, pg_engine, monkeypatch, world, service):
    from app.core import config
    base = config.get_settings()
    ingest_direct(pg_engine, 2)
    monkeypatch.setattr(config, "get_settings", lambda: base)
    monkeypatch.setattr("app.api.ops.get_settings", lambda: dataclasses.replace(base, metrics_token=None))
    assert api.get("/metrics").status_code == 404  # disabled by default
    token = "m" * 40
    monkeypatch.setattr("app.api.ops.get_settings", lambda: dataclasses.replace(base, metrics_token=token))
    assert api.get("/metrics").status_code == 401
    assert api.get("/metrics", headers=auth.headers("admin")).status_code == 401  # a user JWT isn't enough
    body = api.get("/metrics", headers={"Authorization": f"Bearer {token}"}).text
    assert 'integrationlab_events_pending{tenant="meridian"} 2.0' in body
    assert "# TYPE integrationlab_workers_healthy gauge" in body


# --------------------------------------------------------------------------- #
# Audit log
# --------------------------------------------------------------------------- #
def test_audit_log_is_append_only(pg_engine):
    PostgresAuditRepository(pg_engine).record(
        __import__("app.repositories.audit", fromlist=["x"]).AuditEntry(actor="a", action="test.action",
                                                                        outcome="success", tenant_id="meridian"))
    for sql in ("UPDATE audit_log SET actor = 'forged'", "DELETE FROM audit_log", "TRUNCATE audit_log"):
        with pytest.raises(DBAPIError, match="append-only"), pg_engine.begin() as c:
            c.execute(text(sql))
    assert audit_rows(pg_engine)[0]["actor"] == "a"


def test_audit_endpoint_admin_only_and_tenant_scoped(api, auth, pg_engine, world, service):
    eid = dead_letter_event(pg_engine, world, service)
    api.post(f"/webhooks/events/{eid}/replay", headers=auth.headers("operator", sub="op1"))
    hdead = dead_letter_event(pg_engine, world, service, "HB-2001", tenant="harbor")
    api.post(f"/webhooks/events/{hdead}/replay", headers=auth.headers("operator", "harbor", sub="hop"))
    assert api.get("/admin/audit", headers=auth.headers("operator")).status_code == 403
    mine = api.get("/admin/audit", headers=auth.headers("admin")).json()
    assert [(e["actor"], e["action"], e["outcome"]) for e in mine] == [("op1", "webhook_event.replay", "success")]
    assert api.get("/admin/audit?action=webhook_event.replay",
                   headers=auth.headers("admin", "harbor")).json()[0]["actor"] == "hop"


def test_mapping_publish_is_audited(api, auth, pg_engine):
    import json
    from tests.support.data import MAPPING
    from app.repositories.tenants import PostgresTenantRepository
    previous = app.dependency_overrides.get(deps.get_tenant_repository)
    app.dependency_overrides[deps.get_tenant_repository] = lambda: PostgresTenantRepository(pg_engine)
    try:
        assert api.put("/admin/mapping", json=MAPPING, headers=auth.headers("admin", sub="adm")).status_code == 201
        assert api.put("/admin/mapping", json={**MAPPING, "tenant_id": "harbor"},
                       headers=auth.headers("admin", sub="adm")).status_code == 422
        assert api.put("/admin/mapping", json=MAPPING, headers=auth.headers("operator", sub="op")).status_code == 403
    finally:
        app.dependency_overrides[deps.get_tenant_repository] = previous  # restore the auth fixture's directory
    rows = audit_rows(pg_engine, "mapping.publish")
    assert [(r["actor"], r["outcome"]) for r in rows] == [("adm", "success"), ("adm", "rejected"), ("op", "denied")]
    assert "fields" in rows[0]["details"] and json.dumps(rows) .count("secret") == 0


def test_audit_logs_contain_no_secrets_or_tokens(api, auth, pg_engine, world, service, caplog):
    eid = dead_letter_event(pg_engine, world, service)
    headers = auth.headers("operator")
    with caplog.at_level("DEBUG"):
        api.post(f"/webhooks/events/{eid}/replay", headers=headers, json={"reason": "ok"})
    token = headers["Authorization"].split()[1]
    assert token not in caplog.text
    assert '"action": "webhook_event.replay"' in caplog.text  # structured audit log line emitted


# --------------------------------------------------------------------------- #
# Retention
# --------------------------------------------------------------------------- #
def _age(engine, eid, days, column="processed_at"):
    extra = ", processed_at = now() - make_interval(days => :d)" if column == "processed_at" else ""
    with engine.begin() as c:
        c.execute(text("UPDATE webhook_events SET received_at = now() - make_interval(days => :d)" + extra +
                       " WHERE id = :e"), {"d": days, "e": eid})


def test_retention_dry_run_counts_only_eligible_rows(pg_engine, world, service):
    old_ok, new_ok = ingest_direct(pg_engine, 2)
    build_worker(pg_engine, service).run(max_events=2, max_seconds=5)
    _age(pg_engine, old_ok, 40)
    old_dead = dead_letter_event(pg_engine, world, service)
    _age(pg_engine, old_dead, 400)
    body = event_body(event_type="contact.updated")
    ignored = str(ingestor(pg_engine).ingest("meridian", signed(body)["X-IntegrationLab-Signature"], body).result.id)
    _age(pg_engine, ignored, 10, "processed_at")
    with pg_engine.begin() as c:  # an old sync record no event references, and one that is referenced
        c.execute(text("UPDATE sync_records SET started_at = now() - interval '200 days', "
                       "finished_at = now() - interval '200 days'"))
    with pg_engine.connect() as c:
        unreferenced = c.execute(text(
            "SELECT count(*) FROM sync_records s WHERE NOT EXISTS "
            "(SELECT 1 FROM webhook_events w WHERE w.sync_record_id = s.id)")).scalar()
        referenced = c.execute(text("SELECT count(DISTINCT sync_record_id) FROM webhook_events")).scalar()
    report = retention.plan(pg_engine, RetentionSettings())
    assert report.counts["succeeded_events"] == {"meridian": 1}
    assert report.counts["ignored_events"] == {"meridian": 1}
    # Only records no event references (e.g. a superseded retry attempt) are eligible.
    assert referenced >= 3 and unreferenced >= 1
    assert report.counts["sync_records"] == {"meridian": unreferenced}
    assert len(by_id(pg_engine)) == 4  # dry run deleted nothing


def test_retention_execute_refuses_when_disabled(pg_engine):
    with pytest.raises(retention.RetentionDisabled):
        retention.execute(pg_engine, RetentionSettings(enabled=False), actor="test")


def test_retention_execute_deletes_only_eligible_and_audits(pg_engine, world, service):
    old_ok, keep_ok = ingest_direct(pg_engine, 2)
    build_worker(pg_engine, service).run(max_events=2, max_seconds=5)
    _age(pg_engine, old_ok, 40)
    old_dead = dead_letter_event(pg_engine, world, service)
    _age(pg_engine, old_dead, 400)
    [old_pending] = ingest_direct(pg_engine, 1)
    _age(pg_engine, old_pending, 400, "received_at")
    old_ok_record = by_id(pg_engine)[old_ok]["sync_record_id"]
    with pg_engine.begin() as c:
        c.execute(text("UPDATE sync_records SET started_at = now() - interval '200 days', "
                       "finished_at = now() - interval '200 days' WHERE id = :r"), {"r": old_ok_record})

    report = retention.execute(pg_engine, RetentionSettings(enabled=True, batch_size=10), actor="cli:test")
    remaining = by_id(pg_engine)
    assert old_ok not in remaining                                      # purged
    assert {keep_ok, old_dead, old_pending} <= set(remaining)           # kept (recent / needs action)
    assert attempts(pg_engine, old_ok) == []                            # attempt history cascaded
    assert report.counts["succeeded_events"] == {"meridian": 1}
    # The old record became unreferenced once its event was purged in the same run.
    assert report.counts["sync_records"] == {"meridian": 1}
    [a] = audit_rows(pg_engine, "retention.purge")
    assert (a["actor"], a["tenant_id"], a["details"]["succeeded_events"]) == ("cli:test", "meridian", 1)


# --------------------------------------------------------------------------- #
# Database outages on the ops API
# --------------------------------------------------------------------------- #
def test_ops_endpoints_return_503_when_database_down(api, auth, dead_engine):
    app.dependency_overrides[deps.get_event_repository] = lambda: PostgresWebhookEventRepository(dead_engine)
    app.dependency_overrides[deps.get_operations_repository] = lambda: PostgresOperationsRepository(dead_engine)
    dead_audit = PostgresAuditRepository(dead_engine)
    app.dependency_overrides[deps.get_audit_repository] = lambda: dead_audit
    assert api.post(f"/webhooks/events/{uuid.uuid4()}/replay", headers=auth.headers("operator")).status_code == 503
    assert api.post("/webhooks/events/replay", json={}, headers=auth.headers("operator")).status_code == 503
    assert api.get("/ops/metrics", headers=auth.headers("viewer")).status_code == 503
    assert api.get("/admin/audit", headers=auth.headers("admin")).status_code == 503


def test_unhandled_database_errors_use_accurate_generic_messages(api, auth, dead_engine):
    """Routes without their own handling fall back to app-wide messages that don't name the wrong store."""
    app.dependency_overrides[deps.get_event_repository] = lambda: PostgresWebhookEventRepository(dead_engine)
    r = api.get("/webhooks/events", headers=auth.headers("viewer"))
    assert r.status_code == 503 and r.json() == {"detail": "database unavailable; retry later"}
    assert "history" not in r.text.lower()
