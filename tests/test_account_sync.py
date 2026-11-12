"""End-to-end synchronization: real clients -> both mock services, in-process."""

from __future__ import annotations

import logging

import pytest

from app.integrations.internal_client import InternalSystemClient
from tests.support.mappings import MappingRegistry
from tests.support.fakes import InMemorySyncHistoryRepository
from app.schemas.sync import SyncStage, SyncStatus
from app.services.account_sync import AccountSyncService, OwnershipViolationError, _assert_crm_owned_only
from tests.support.clients import cms_headers, cms_settings, crm_patch
from tests.support.data import MAPPING_DIR
from tests.support.concurrency import join_all, start_threads
from tests.support.fakes import InProcessSyncLocks, StaticConnections

T = "meridian"


def cms_customer(cms_app, external_id):
    r = cms_app.get(f"/api/v1/customers/by-external-id/{external_id}", headers=cms_headers())
    return r.json() if r.status_code == 200 else None


def staff_update(cms_app, customer_id, **fields):
    """Meridian staff edit internal-owned fields through the CMS's own API."""
    r = cms_app.patch(f"/api/v1/customers/{customer_id}", json=fields, headers=cms_headers())
    assert r.status_code == 200, r.text
    return r.json()


# --- Happy paths ------------------------------------------------------------ #
def test_first_sync_creates_customer(sync_service, cms_app, history):
    result = sync_service.sync_account(T, "SF-1001")

    assert result.status is SyncStatus.CREATED and result.succeeded
    customer = cms_customer(cms_app, "SF-1001")
    assert customer["customer_id"] == result.destination_customer_id
    assert customer["legal_name"] == "Acme Capital"
    assert customer["industry_segment"] == "FINANCIAL_SERVICES"
    assert customer["onboarding_status"] == "pending"  # destination default, not written by us
    assert result.mapping_version == 1
    assert history.list_recent(T) == [result]


def test_crm_change_updates_only_changed_fields(sync_service, crm_app):
    sync_service.sync_account(T, "SF-1002")
    crm_patch(crm_app, "SF-1002", AnnualRevenue=9000000)

    result = sync_service.sync_account(T, "SF-1002")

    assert result.status is SyncStatus.UPDATED
    assert result.fields_changed == ["annual_revenue_usd", "crm_last_modified_at"]


def test_duplicate_sync_is_idempotent_and_writes_nothing(sync_service, cms_app):
    first = sync_service.sync_account(T, "SF-1001")
    writes_after_first = cms_app.app.state.cms.write_count

    second = sync_service.sync_account(T, "SF-1001")
    third = sync_service.sync_account(T, "SF-1001")

    assert first.status is SyncStatus.CREATED
    assert second.status is SyncStatus.UNCHANGED and third.status is SyncStatus.UNCHANGED
    assert second.destination_customer_id == first.destination_customer_id
    assert cms_app.app.state.cms.write_count == writes_after_first
    assert len(cms_app.app.state.cms.customers) == 1


def test_concurrent_duplicate_syncs_create_one_customer(sync_service, cms_app):
    results = []
    threads = start_threads([lambda: results.append(sync_service.sync_account(T, "SF-1001"))] * 8)
    join_all(threads, timeout=60, what="8 concurrent syncs of SF-1001")

    assert len(cms_app.app.state.cms.customers) == 1
    assert sorted(r.status.value for r in results).count("created") == 1
    assert all(r.succeeded for r in results)


def test_create_race_with_other_writer_falls_back_to_update(crm_client, cms_app, history):
    """Another writer creates the customer between our lookup and our create."""

    class RacyDestination(InternalSystemClient):
        raced = False

        def get_customer_by_external_id(self, external_crm_id):
            if not self.raced:
                self.raced = True
                cms_app.post("/api/v1/customers", headers=cms_headers(), json={
                    "external_crm_id": external_crm_id, "legal_name": "Old Name",
                    "relationship_manager": "Someone"})
                return None
            return super().get_customer_by_external_id(external_crm_id)

    service = AccountSyncService(
        connections=StaticConnections(crm_client, RacyDestination(cms_settings(), http_client=cms_app)),
        mappings=MappingRegistry(MAPPING_DIR), history=history, locks=InProcessSyncLocks())
    result = service.sync_account(T, "SF-1001")

    assert result.status is SyncStatus.UPDATED
    assert "legal_name" in result.fields_changed
    assert len(cms_app.app.state.cms.customers) == 1
    assert cms_customer(cms_app, "SF-1001")["legal_name"] == "Acme Capital"


# --- Field ownership --------------------------------------------------------- #
def test_destination_owned_fields_preserved_across_sync(sync_service, crm_app, cms_app):
    created = sync_service.sync_account(T, "SF-1002")
    staff_update(cms_app, created.destination_customer_id,
                 onboarding_status="approved", risk_classification="high")

    crm_patch(crm_app, "SF-1002", Name="Meridian Holdings Group", OwnerName="Dana Lee")
    result = sync_service.sync_account(T, "SF-1002")

    assert result.status is SyncStatus.UPDATED
    customer = cms_customer(cms_app, "SF-1002")
    assert customer["legal_name"] == "Meridian Holdings Group"
    assert customer["relationship_manager"] == "Dana Lee"
    assert customer["onboarding_status"] == "approved"
    assert customer["risk_classification"] == "high"
    assert not {"onboarding_status", "risk_classification"} & set(result.fields_changed)


def test_crm_owned_field_drift_in_destination_is_corrected(sync_service, cms_app):
    created = sync_service.sync_account(T, "SF-1001")
    staff_update(cms_app, created.destination_customer_id, legal_name="Manually Edited",
                 risk_classification="medium")

    result = sync_service.sync_account(T, "SF-1001")

    assert result.fields_changed == ["legal_name"]
    customer = cms_customer(cms_app, "SF-1001")
    assert customer["legal_name"] == "Acme Capital"          # CRM wins for CRM-owned
    assert customer["risk_classification"] == "medium"        # destination keeps its own


def test_ownership_guard_blocks_destination_owned_writes():
    with pytest.raises(OwnershipViolationError):
        _assert_crm_owned_only({"legal_name": "x", "onboarding_status": "approved"})
    with pytest.raises(OwnershipViolationError):
        _assert_crm_owned_only({"customer_id": "x"})


def test_cleared_crm_field_clears_destination(sync_service, crm_app, cms_app):
    sync_service.sync_account(T, "SF-1001")
    crm_app.put("/_admin/accounts/SF-1001", json={
        "Name": "Acme Capital", "OwnerName": "Sarah Johnson", "Industry": None,
        "AnnualRevenue": 2500000, "LastModifiedDate": "2026-11-05T00:00:00+00:00"})

    result = sync_service.sync_account(T, "SF-1001")

    assert "industry_segment" in result.fields_changed
    assert cms_customer(cms_app, "SF-1001")["industry_segment"] is None


def test_stale_source_does_not_overwrite_newer_destination(sync_service, crm_app, cms_app):
    crm_patch(crm_app, "SF-1001", Name="Newer Name")
    sync_service.sync_account(T, "SF-1001")
    crm_app.put("/_admin/accounts/SF-1001", json={
        "Name": "Older Name", "OwnerName": "Sarah Johnson",
        "LastModifiedDate": "2020-01-01T00:00:00+00:00"})

    result = sync_service.sync_account(T, "SF-1001")

    assert result.status is SyncStatus.SKIPPED_STALE
    assert cms_customer(cms_app, "SF-1001")["legal_name"] == "Newer Name"


# --- Failures ----------------------------------------------------------------- #
def test_destination_outage_reports_retryable_failure(sync_service, cms_app, history):
    cms_app.put("/_admin/faults", json={"mode": "error", "remaining": 100, "status_code": 503})

    result = sync_service.sync_account(T, "SF-1001")

    assert result.status is SyncStatus.FAILED and not result.succeeded
    assert result.stage is SyncStage.LOAD_DESTINATION
    assert result.error.code == "destination_unavailable"
    assert result.error.retryable is True
    assert result.error.upstream_status == 503
    assert history.list_recent(T, status=SyncStatus.FAILED) == [result]
    assert cms_app.app.state.cms.customers == {}


def test_destination_write_outage_after_successful_read(sync_service, cms_app):
    cms_app.put("/_admin/faults", json={"mode": "error", "remaining": 100, "writes_only": True})
    result = sync_service.sync_account(T, "SF-1001")
    assert result.stage is SyncStage.LOAD_DESTINATION and result.error.retryable


def test_destination_recovers_and_retry_succeeds(sync_service, cms_app, history):
    cms_app.put("/_admin/faults", json={"mode": "error", "remaining": 100})
    failed = sync_service.sync_account(T, "SF-1001")
    cms_app.put("/_admin/faults", json={"mode": "none"})

    retried = sync_service.sync_account(T, "SF-1001")

    assert failed.status is SyncStatus.FAILED
    assert retried.status is SyncStatus.CREATED
    assert [r.status for r in history.list_recent(T, crm_account_id="SF-1001")] == [
        SyncStatus.CREATED, SyncStatus.FAILED]


def test_destination_auth_failure_is_permanent(crm_client, cms_app, history):
    service = AccountSyncService(
        connections=StaticConnections(
            crm_client, InternalSystemClient(cms_settings(api_key="wrong"), http_client=cms_app)),
        mappings=MappingRegistry(MAPPING_DIR), history=history, locks=InProcessSyncLocks())
    result = service.sync_account(T, "SF-1001")
    assert result.error.code == "destination_auth_failed" and result.error.retryable is False


def test_crm_outage_fails_at_fetch_stage(sync_service, crm_app, cms_app):
    crm_app.put("/_admin/faults", json={"mode": "error", "remaining": 100, "status_code": 502})
    result = sync_service.sync_account(T, "SF-1001")
    assert result.stage is SyncStage.FETCH_SOURCE
    assert result.error.code == "crm_unavailable" and result.error.retryable
    assert crm_app.app.state.crm.account_reads == 3  # bounded: 1 + 2 retries
    assert cms_app.app.state.cms.write_count == 0


def test_crm_not_found(sync_service):
    result = sync_service.sync_account(T, "SF-404")
    assert result.stage is SyncStage.FETCH_SOURCE
    assert result.error.code == "crm_not_found" and not result.error.retryable


def test_missing_required_field_fails_at_transform(sync_service, crm_app, cms_app):
    crm_app.put("/_admin/accounts/SF-BAD", json={"Name": "", "Industry": "Banking"})
    result = sync_service.sync_account(T, "SF-BAD")
    assert result.stage is SyncStage.TRANSFORM
    assert result.error.code == "mapping_failed" and not result.error.retryable
    assert {i["field"] for i in result.error.issues} == {"legal_name", "relationship_manager"}
    assert cms_app.app.state.cms.write_count == 0


def test_unknown_tenant_mapping_fails_before_network(sync_service, crm_app):
    result = sync_service.sync_account("no-such-tenant", "SF-1001")
    assert result.error.code == "mapping_config_invalid"
    assert crm_app.app.state.crm.account_reads == 0


def test_unexpected_exception_is_recorded_not_swallowed(crm_client, cms_client, history, caplog):
    class Boom(MappingRegistry):
        def get(self, tenant_id):
            raise RuntimeError("bug")

    service = AccountSyncService(connections=StaticConnections(crm_client, cms_client),
                                 mappings=Boom(MAPPING_DIR), history=history, locks=InProcessSyncLocks())
    with caplog.at_level(logging.ERROR):
        result = service.sync_account(T, "SF-1001")
    assert result.stage is SyncStage.INTERNAL and result.error.code == "unexpected_error"
    assert history.list_recent(T) == [result]
    assert "sync.unexpected_error" in caplog.text


# --- History & logging -------------------------------------------------------- #
def test_history_is_tenant_scoped(sync_service, history):
    rec = sync_service.sync_account(T, "SF-1001")
    sync_service.sync_account("no-such-tenant", "SF-1001")
    assert [r.id for r in history.list_recent(T)] == [rec.id]
    assert history.get("no-such-tenant", rec.id) is None
    assert history.get(T, rec.id) == rec


def test_history_is_bounded():
    repo = InMemorySyncHistoryRepository(max_records=3)
    from datetime import datetime, timezone

    from app.schemas.sync import SyncRecord
    now = datetime.now(timezone.utc)
    for i in range(5):
        repo.add(SyncRecord(tenant_id=T, crm_account_id=str(i), status=SyncStatus.UNCHANGED,
                            started_at=now, finished_at=now, duration_ms=0))
    assert [r.crm_account_id for r in repo.list_recent(T)] == ["4", "3", "2"]


def test_sync_logs_and_history_exclude_account_data(sync_service, crm_app, caplog):
    crm_patch(crm_app, "SF-1001", Name="Confidential Name Corp", AnnualRevenue=123456789)
    with caplog.at_level(logging.DEBUG):
        result = sync_service.sync_account(T, "SF-1001")
    dumped = result.model_dump_json()
    for secret in ("Confidential Name Corp", "123456789", "Sarah Johnson"):
        assert secret not in caplog.text
        assert secret not in dumped
