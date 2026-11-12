"""In-memory implementations of the app's protocols, for fast unit tests only.

Production always uses PostgreSQL (``app/wiring.py``). These fakes implement the
same protocols (``SyncHistoryRepository``, ``TenantRepository``,
``AuditRepository``) with the same tenant-scoping rules, so unit tests can run
without a database. Contract parity with PostgreSQL is covered by
``tests/integration/test_pg_history.py::test_same_semantics_as_in_memory_repository``.
"""

from __future__ import annotations

import copy
import threading
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from app.repositories.audit import AuditEntry, emit_log
from app.repositories.tenants import MappingVersion, Tenant, TenantIntegration
from app.db.errors import RepositoryError
from app.integrations.crm_client import CRMClient
from app.integrations.internal_client import InternalSystemClient
from app.services.locks import LockHandle, advisory_lock_key
from app.services.tenant_context import TenantClients
from app.schemas.sync import SyncRecord, SyncStatus


class InMemorySyncHistoryRepository:
    """Thread-safe, bounded, process-local. Not durable: lost on restart."""

    def __init__(self, max_records: int = 10_000) -> None:
        self._records: deque[SyncRecord] = deque(maxlen=max_records)
        self._lock = threading.Lock()

    def add(self, record: SyncRecord) -> None:
        with self._lock:
            self._records.append(record)

    def get(self, tenant_id: str, record_id: UUID) -> SyncRecord | None:
        with self._lock:
            return next(
                (r for r in self._records if r.id == record_id and r.tenant_id == tenant_id), None
            )

    def list_recent(
        self,
        tenant_id: str,
        *,
        limit: int = 50,
        crm_account_id: str | None = None,
        status: SyncStatus | None = None,
    ) -> list[SyncRecord]:
        limit = max(1, min(limit, 500))
        out: list[SyncRecord] = []
        with self._lock:
            for r in reversed(self._records):
                if r.tenant_id != tenant_id:
                    continue
                if crm_account_id is not None and r.crm_account_id != crm_account_id:
                    continue
                if status is not None and r.status is not status:
                    continue
                out.append(r)
                if len(out) >= limit:
                    break
        return out


class InMemoryTenantRepository:
    def __init__(self) -> None:
        self._tenants: dict[str, Tenant] = {}
        self._integrations: dict[str, TenantIntegration] = {}
        self._mappings: dict[str, list[MappingVersion]] = {}
        self._lock = threading.Lock()

    # seeding helpers (not part of the protocol)
    def put_tenant(self, tenant: Tenant) -> None:
        self._tenants[tenant.id] = tenant

    def put_integration(self, integration: TenantIntegration) -> None:
        self._integrations[integration.tenant_id] = integration

    def get_tenant(self, tenant_id: str) -> Tenant | None:
        return self._tenants.get(tenant_id)

    def get_integration(self, tenant_id: str) -> TenantIntegration | None:
        return self._integrations.get(tenant_id)

    def get_active_mapping(self, tenant_id: str) -> MappingVersion | None:
        versions = self._mappings.get(tenant_id)
        return copy.deepcopy(versions[-1]) if versions else None

    def list_mapping_versions(self, tenant_id: str) -> list[MappingVersion]:
        return copy.deepcopy(list(reversed(self._mappings.get(tenant_id, []))))

    def add_mapping_version(self, tenant_id: str, config: dict[str, Any], created_by: str) -> MappingVersion:
        with self._lock:
            if tenant_id not in self._tenants:
                raise RepositoryError("unknown tenant")
            versions = self._mappings.setdefault(tenant_id, [])
            version = (versions[-1].version if versions else 0) + 1
            stored = MappingVersion(tenant_id, version, {**copy.deepcopy(config), "version": version},
                                    created_by, datetime.now(timezone.utc))
            versions.append(stored)
            return copy.deepcopy(stored)


class InMemoryAuditRepository:
    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []
        self._lock = threading.Lock()

    def record(self, entry: AuditEntry) -> None:
        stamped = AuditEntry(**{**asdict(entry), "occurred_at": entry.occurred_at or datetime.now(timezone.utc)})
        with self._lock:
            self.entries.append(stamped)
        emit_log(stamped)

    def list_recent(self, tenant_id: str, *, limit: int = 100, action: str | None = None) -> list[AuditEntry]:
        rows = [e for e in reversed(self.entries) if e.tenant_id == tenant_id and (not action or e.action == action)]
        return rows[: max(1, min(limit, 500))]


class _AlwaysHeld:
    def verify(self) -> None:
        return None


class InProcessSyncLocks:
    """Bounded striped locks. Serialises within one process only."""

    def __init__(self, stripes: int = 64) -> None:
        self._locks = [threading.Lock() for _ in range(stripes)]

    @contextmanager
    def hold(self, tenant_id: str, crm_account_id: str) -> Iterator[LockHandle]:
        lock = self._locks[advisory_lock_key(tenant_id, crm_account_id) % len(self._locks)]
        with lock:
            yield _AlwaysHeld()


class StaticConnections:
    """One fixed pair of clients for every tenant (single-tenant unit tests)."""

    def __init__(self, crm: CRMClient, destination: InternalSystemClient) -> None:
        self._clients = TenantClients(crm, destination)

    def for_tenant(self, tenant_id: str) -> TenantClients:
        return self._clients
