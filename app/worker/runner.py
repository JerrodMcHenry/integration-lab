"""Background worker: processes persisted webhook events, independent of the API.

Guarantees:
- **No event is silently lost.** A claimed event either gets an outcome
  recorded (succeeded/failed, with error metadata) or keeps its
  ``processing`` lease. When the lease expires, any worker reclaims it.
  Claims are bounded by ``max_attempts``.
- **Safe concurrency.** Claims use ``FOR UPDATE SKIP LOCKED``, so concurrent
  workers never claim the same event. Two events for the same account are
  serialised by the sync service's per-account advisory lock.
- **No DB transaction during HTTP calls.** Claim and complete are separate
  short transactions. The sync's lock connection is idle, not in a transaction.
- **Bounded execution.** Each event is bounded by client timeouts and bounded
  retries. The lease must exceed the worst-case sync duration (checked at
  startup). Batches are small, and the loop stops on signal after the current event.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from app.core.config import Settings
from app.repositories.sync_history import RepositoryError
from app.repositories.tenants import TenantRepository
from app.repositories.webhook_events import ClaimedEvent, Outcome, PostgresWebhookEventRepository
from app.schemas.sync import SyncStatus
from app.services.account_sync import AccountSyncService, HistoryPersistenceError

logger = logging.getLogger(__name__)


class WorkerConfigurationError(RuntimeError):
    pass


@dataclass
class RunStats:
    claimed: int = 0
    succeeded: int = 0
    failed: int = 0
    lease_lost: int = 0
    completion_errors: int = 0
    claim_errors: int = 0


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


def resolve_lease_seconds(settings: Settings) -> float:
    from app.api.deps import worst_case_sync_seconds

    worst = worst_case_sync_seconds(settings.crm, settings.internal)
    # The sync may also wait up to lock_timeout for another worker holding the same account.
    needed = worst + settings.database.lock_timeout_seconds
    lease = settings.worker.lease_seconds or max(60.0, 2 * needed)
    if lease < needed:
        raise WorkerConfigurationError(
            f"WORKER_LEASE_SECONDS={lease:.0f} is shorter than the worst-case event duration "
            f"({needed:.0f}s); events would be reclaimed while still being processed")
    return lease


class EventWorker:
    def __init__(self, *, events: PostgresWebhookEventRepository, service: AccountSyncService,
                 tenants: TenantRepository, lease_seconds: float, batch_size: int, max_attempts: int,
                 poll_interval: float, worker_id: str | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._events = events
        self._service = service
        self._tenants = tenants
        self._lease = lease_seconds
        self._batch = batch_size
        self._max_attempts = max_attempts
        self._poll = poll_interval
        self.worker_id = worker_id or default_worker_id()
        self._sleep = sleep
        self._stop = threading.Event()
        self.stats = RunStats()

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ #
    def run_once(self) -> int:
        """Claim one batch and process it. Returns the number of events claimed."""
        try:
            claimed = self._events.claim(self.worker_id, batch=self._batch, lease_seconds=self._lease,
                                         max_attempts=self._max_attempts)
        except RepositoryError as exc:
            self.stats.claim_errors += 1
            logger.error("worker.claim_failed worker=%s error=%s", self.worker_id, exc)
            raise
        self.stats.claimed += len(claimed)
        for event in claimed:
            if self._stop.is_set():
                # Unprocessed claims keep their lease and are reclaimed after it expires.
                logger.info("worker.stopping_with_claims worker=%s remaining_event=%s", self.worker_id, event.id)
                break
            self._process(event)
        return len(claimed)

    def run(self, *, max_events: int | None = None, max_seconds: float | None = None) -> RunStats:
        """Loop until stopped (or until a bound is reached). DB outages back off, never crash."""
        started = time.monotonic()
        backoff = self._poll
        logger.info("worker.started worker=%s lease=%.0fs batch=%d", self.worker_id, self._lease, self._batch)
        while not self._stop.is_set():
            if max_events is not None and self.stats.claimed >= max_events:
                break
            if max_seconds is not None and time.monotonic() - started >= max_seconds:
                break
            try:
                n = self.run_once()
                backoff = self._poll
                if n == 0:
                    self._stop.wait(self._poll)
            except RepositoryError:
                backoff = min(backoff * 2, 30.0)
                self._stop.wait(backoff)
        logger.info("worker.stopped worker=%s stats=%s", self.worker_id, self.stats)
        return self.stats

    # ------------------------------------------------------------------ #
    def _process(self, event: ClaimedEvent) -> None:
        logger.info("worker.processing worker=%s event=%s tenant=%s account=%s attempt=%d",
                    self.worker_id, event.id, event.tenant_id, event.crm_account_id, event.attempts)
        outcome = self._run_sync(event)
        try:
            recorded = self._events.complete(event, outcome)
        except RepositoryError as exc:
            # The sync is idempotent: when the lease expires the event is reclaimed and re-run safely.
            self.stats.completion_errors += 1
            logger.error("worker.complete_failed worker=%s event=%s error=%s (event stays 'processing' "
                         "until its lease expires, then is reclaimed)", self.worker_id, event.id, exc)
            return
        if not recorded:
            self.stats.lease_lost += 1
            logger.warning("worker.lease_lost worker=%s event=%s (another worker owns it; result discarded)",
                           self.worker_id, event.id)
            return
        if outcome.status == "succeeded":
            self.stats.succeeded += 1
        else:
            self.stats.failed += 1
        logger.info("worker.completed worker=%s event=%s status=%s error=%s", self.worker_id, event.id,
                    outcome.status, outcome.error_code or "-")

    def _run_sync(self, event: ClaimedEvent) -> Outcome:
        if not event.crm_account_id:
            return Outcome("failed", error_code="event_missing_account", error_message="event has no account id",
                           error_retryable=False)
        try:
            tenant = self._tenants.get_tenant(event.tenant_id)
        except RepositoryError as exc:
            return Outcome("failed", error_code="tenant_lookup_failed", error_message=str(exc), error_retryable=True)
        if tenant is None or not tenant.active:
            return Outcome("failed", error_code="tenant_inactive",
                           error_message="tenant is suspended or no longer provisioned", error_retryable=False)
        try:
            # Same code path as POST /sync: mapping, tenant connections, advisory lock, history.
            record = self._service.sync_account(event.tenant_id, event.crm_account_id)
        except HistoryPersistenceError as exc:
            return Outcome("failed", error_code="history_not_persisted",
                           error_message=f"sync ran (status={exc.record.status.value}) but history was not saved",
                           error_retryable=True)
        except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
            logger.exception("worker.unexpected_error worker=%s event=%s", self.worker_id, event.id)
            return Outcome("failed", error_code="worker_error", error_message=type(exc).__name__,
                           error_retryable=False)
        if record.status is SyncStatus.FAILED:
            err = record.error
            return Outcome("failed", sync_record_id=record.id, error_code=err.code, error_message=err.message,
                           error_retryable=err.retryable)
        return Outcome("succeeded", sync_record_id=record.id)
