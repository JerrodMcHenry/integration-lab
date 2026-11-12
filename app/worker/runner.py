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
- **Safe processing deadline (M7).** The worker notes a monotonic timestamp
  *before* claiming. The server's lease can't expire before
  ``t_claim + lease``. Before every destination write the sync calls a guard
  that refuses to write unless ``now + write_margin < t_claim + lease``, where
  ``write_margin`` is the worst-case duration of one destination call. A
  stalled worker (GC pause, suspended VM, slow lock wait) therefore never
  writes after another worker could have reclaimed the event. Outcomes are
  fenced by ``claim_token`` (M6).
- **Retries (M7).** Retryable failures are rescheduled with bounded
  exponential backoff with jitter. They go to ``dead_letter`` when attempts run
  out. Permanent failures go to ``failed`` and are never retried automatically.
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

from app.core.config import RetrySettings, Settings
from app.repositories.sync_history import RepositoryError
from app.repositories.tenants import TenantRepository
from app.repositories.webhook_events import ClaimedEvent, Outcome, PostgresWebhookEventRepository
from app.schemas.sync import SyncStatus
from app.services.account_sync import AccountSyncService, HistoryPersistenceError, OwnershipLost
from app.worker.retry_policy import RetryPolicy

logger = logging.getLogger(__name__)


class WorkerConfigurationError(RuntimeError):
    pass


@dataclass
class RunStats:
    claimed: int = 0
    succeeded: int = 0
    failed: int = 0
    retried: int = 0
    dead_lettered: int = 0
    lease_lost: int = 0
    completion_errors: int = 0
    claim_errors: int = 0


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


def destination_write_margin(settings: Settings) -> float:
    """Worst-case duration of ONE destination call (timeouts, in-client retries, backoff)."""
    c = settings.internal
    return (c.max_retries + 1) * c.timeout_seconds + c.max_retries * c.backoff_max_seconds


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
                 sleep: Callable[[float], None] = time.sleep,
                 retry_policy: RetryPolicy | None = None, write_margin_seconds: float = 0.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
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
        self._retry = retry_policy or RetryPolicy(RetrySettings(), max_attempts)
        self._margin = write_margin_seconds
        self._clock = clock
        self._deadline: float | None = None  # lease-derived deadline of the batch being processed
        self._hostname = socket.gethostname()
        if self._margin >= self._lease:
            raise WorkerConfigurationError("lease must be longer than the destination write margin")

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ #
    def run_once(self) -> int:
        """Claim one batch and process it. Returns the number of events claimed."""
        self._beat("idle")
        claim_started = self._clock()  # taken BEFORE the claim: a conservative lease start
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
            self._deadline = claim_started + self._lease
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
                self._beat("backing_off")
                self._stop.wait(backoff)
        self._beat("stopped")
        logger.info("worker.stopped worker=%s stats=%s", self.worker_id, self.stats)
        return self.stats

    def _beat(self, state: str, current_event=None) -> None:
        """Best-effort liveness record; never interrupts processing."""
        try:
            self._events.heartbeat(worker_id=self.worker_id, hostname=self._hostname, pid=os.getpid(),
                                   state=state, current_event_id=current_event,
                                   succeeded=self.stats.succeeded, failed=self.stats.failed + self.stats.dead_lettered)
        except Exception as exc:  # noqa: BLE001
            logger.warning("worker.heartbeat_failed worker=%s error=%s", self.worker_id, type(exc).__name__)

    def _guard_for(self, deadline: float) -> Callable[[], None]:
        def guard() -> None:
            if self._clock() + self._margin >= deadline:
                raise OwnershipLost("processing deadline reached: the event lease may have expired; "
                                    "refusing to write to the destination")
        return guard

    # ------------------------------------------------------------------ #
    def _process(self, event: ClaimedEvent) -> None:
        logger.info("worker.processing worker=%s event=%s tenant=%s account=%s attempt=%d",
                    self.worker_id, event.id, event.tenant_id, event.crm_account_id, event.attempts)
        self._beat("processing", event.id)
        deadline = self._deadline if self._deadline is not None else self._clock() + self._lease
        outcome = self._decide(event, self._run_sync(event, self._guard_for(deadline)))
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
        elif outcome.status == "pending":
            self.stats.retried += 1
        elif outcome.status == "dead_letter":
            self.stats.dead_lettered += 1
        else:
            self.stats.failed += 1
        logger.info("worker.completed worker=%s event=%s status=%s error=%s retry_in=%s", self.worker_id,
                    event.id, outcome.status, outcome.error_code or "-",
                    f"{outcome.retry_delay_seconds:.1f}s" if outcome.retry_delay_seconds else "-")

    def _decide(self, event: ClaimedEvent, raw: Outcome) -> Outcome:
        decision = self._retry.decide(succeeded=raw.status == "succeeded", retryable=raw.error_retryable,
                                      attempt=event.attempts)
        if decision == "retry":
            return Outcome("pending", raw.sync_record_id, raw.error_code, raw.error_message, True,
                           retry_delay_seconds=self._retry.delay(event.attempts))
        if decision == "dead_letter":
            return Outcome("dead_letter", raw.sync_record_id, raw.error_code, raw.error_message, True)
        return raw

    def _run_sync(self, event: ClaimedEvent, guard: Callable[[], None] | None = None) -> Outcome:
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
            record = self._service.sync_account(event.tenant_id, event.crm_account_id, guard=guard)
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
