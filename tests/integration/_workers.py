"""Top-level functions run in separate OS processes (multiprocessing 'spawn').

Each worker builds its own engine, as an independent application process
would, and shares nothing with the parent except the database (and, for
sync workers, the live mock services).
"""

from __future__ import annotations

import os
import time


def hold_lock(url: str, tenant: str, account: str, hold_seconds: float, barrier_path: str) -> dict:
    from app.db.engine import build_engine
    from app.services.locks import PostgresAdvisorySyncLocks
    from tests.integration.conftest import db_settings

    engine = build_engine(db_settings(url, lock_timeout_seconds=30), application_name="lock-worker")
    locks = PostgresAdvisorySyncLocks(engine, lock_timeout_seconds=30)
    # Wait for the start signal so all processes contend at the same moment.
    while not os.path.exists(barrier_path):
        time.sleep(0.005)
    requested = time.time()
    with locks.hold(tenant, account):
        acquired = time.time()
        time.sleep(hold_seconds)
        released = time.time()
    engine.dispose()
    return {"pid": os.getpid(), "requested": requested, "acquired": acquired, "released": released}


def run_sync(url: str, crm_base: str, cms_base: str, cms_key: str, tenant: str, account: str,
             barrier_path: str) -> dict:
    from app.core.config import CRMSettings, InternalSystemSettings
    from app.db.engine import build_engine
    from app.integrations.crm_client import CRMClient
    from app.integrations.internal_client import InternalSystemClient
    from app.mapping.mapper import MappingRegistry
    from app.repositories.sync_history import PostgresSyncHistoryRepository
    from app.services.account_sync import AccountSyncService
    from app.services.locks import PostgresAdvisorySyncLocks
    from mock_crm import main as mock_crm
    from tests.conftest import MAPPING_DIR
    from tests.integration.conftest import db_settings

    engine = build_engine(db_settings(url, lock_timeout_seconds=30), application_name="sync-worker")
    service = AccountSyncService(
        crm=CRMClient(CRMSettings(base_url=crm_base, token_url=f"{crm_base}/oauth/token",
                                  client_id=mock_crm.CLIENT_ID, client_secret=mock_crm.CLIENT_SECRET,
                                  timeout_seconds=5, max_retries=0, backoff_base_seconds=0,
                                  backoff_max_seconds=0)),
        destination=InternalSystemClient(InternalSystemSettings(
            base_url=cms_base, api_key=cms_key, timeout_seconds=5, max_retries=0,
            backoff_base_seconds=0, backoff_max_seconds=0)),
        mappings=MappingRegistry(MAPPING_DIR),
        history=PostgresSyncHistoryRepository(engine),
        locks=PostgresAdvisorySyncLocks(engine, lock_timeout_seconds=30),
    )
    while not os.path.exists(barrier_path):
        time.sleep(0.005)
    record = service.sync_account(tenant, account)
    engine.dispose()
    return {"pid": os.getpid(), "status": record.status.value, "id": str(record.id),
            "started": record.started_at.timestamp(), "finished": record.finished_at.timestamp()}


def write_then_exit(url: str, tenant: str, account: str) -> str:
    """Persist one record from a process that then exits (restart simulation)."""
    from datetime import datetime, timezone

    from app.db.engine import build_engine
    from app.repositories.sync_history import PostgresSyncHistoryRepository
    from app.schemas.sync import SyncRecord, SyncStatus
    from tests.integration.conftest import db_settings

    engine = build_engine(db_settings(url))
    now = datetime.now(timezone.utc)
    record = SyncRecord(tenant_id=tenant, crm_account_id=account, status=SyncStatus.CREATED,
                        destination_customer_id="CUS-1", fields_changed=["legal_name"],
                        mapping_version=1, started_at=now, finished_at=now, duration_ms=3)
    PostgresSyncHistoryRepository(engine).add(record)
    engine.dispose()
    return str(record.id)
