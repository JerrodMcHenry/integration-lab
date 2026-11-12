"""Synchronization history persistence.

``SyncHistoryRepository`` is the contract the sync service depends on. The
in-memory implementation is temporary; Milestone 4 adds a PostgreSQL-backed
implementation with the same interface. Every read method requires a
``tenant_id`` so that tenant scoping can't be forgotten at a call site.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Protocol
from uuid import UUID

from app.schemas.sync import SyncRecord, SyncStatus


class SyncHistoryRepository(Protocol):
    def add(self, record: SyncRecord) -> None: ...

    def get(self, tenant_id: str, record_id: UUID) -> SyncRecord | None: ...

    def list_recent(
        self,
        tenant_id: str,
        *,
        limit: int = 50,
        crm_account_id: str | None = None,
        status: SyncStatus | None = None,
    ) -> list[SyncRecord]: ...


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
