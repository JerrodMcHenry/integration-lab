"""Operational data: queue metrics and worker heartbeats.

Kept apart from ``webhook_events.py`` (the event lifecycle: ingest, claim,
complete, replay) because these are read-mostly monitoring queries and
liveness writes. They never change an event's state.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.db.errors import translate_db_error


class PostgresOperationsRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # -- metrics ---------------------------------------------------------- #
    def metrics(self, tenant_id: str | None) -> list[dict[str, Any]]:
        """Per-tenant queue metrics (all tenants when ``tenant_id`` is None; platform use only)."""
        where = "WHERE tenant_id = :t" if tenant_id else ""
        sql = f"""
            SELECT tenant_id,
                   count(*) FILTER (WHERE status = 'pending')                                AS pending,
                   count(*) FILTER (WHERE status = 'pending' AND available_at <= now())      AS ready,
                   count(*) FILTER (WHERE status = 'pending' AND attempts > 0)              AS retry_backlog,
                   count(*) FILTER (WHERE status = 'processing')                             AS processing,
                   count(*) FILTER (WHERE status = 'processing' AND lease_expires_at < now()) AS expired_leases,
                   count(*) FILTER (WHERE status = 'failed')                                 AS failed,
                   count(*) FILTER (WHERE status = 'dead_letter')                            AS dead_letter,
                   count(*) FILTER (WHERE status = 'succeeded'
                                      AND processed_at > now() - interval '1 hour')          AS succeeded_1h,
                   extract(epoch FROM now() - min(received_at) FILTER (WHERE status = 'pending')) AS oldest_pending_s,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM processed_at - received_at))
                       FILTER (WHERE status = 'succeeded' AND processed_at > now() - interval '1 hour') AS latency_p50_s,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM processed_at - received_at))
                       FILTER (WHERE status = 'succeeded' AND processed_at > now() - interval '1 hour') AS latency_p95_s,
                   max(extract(epoch FROM processed_at - received_at))
                       FILTER (WHERE status = 'succeeded' AND processed_at > now() - interval '1 hour') AS latency_max_s
              FROM webhook_events {where}
             GROUP BY tenant_id ORDER BY tenant_id
        """
        try:
            with self._engine.connect() as conn:
                return [dict(r._mapping) for r in conn.execute(text(sql), {"t": tenant_id})]
        except SQLAlchemyError as exc:
            raise translate_db_error(exc) from exc
    # -- worker heartbeats -------------------------------------------------- #
    def heartbeat(self, *, worker_id: str, hostname: str, pid: int, state: str,
                  current_event_id: uuid.UUID | None, succeeded: int, failed: int) -> None:
        try:
            with self._engine.begin() as conn:
                conn.execute(text("""
                    INSERT INTO worker_heartbeats (worker_id, hostname, pid, state, current_event_id,
                                                   events_succeeded, events_failed)
                    VALUES (:w, :h, :p, :s, :e, :ok, :ko)
                    ON CONFLICT (worker_id) DO UPDATE
                       SET last_seen_at = now(), state = EXCLUDED.state,
                           current_event_id = EXCLUDED.current_event_id,
                           events_succeeded = EXCLUDED.events_succeeded, events_failed = EXCLUDED.events_failed
                """), {"w": worker_id[:128], "h": hostname[:255], "p": pid, "s": state, "e": current_event_id,
                       "ok": succeeded, "ko": failed})
        except SQLAlchemyError as exc:
            raise translate_db_error(exc) from exc
    def workers(self, *, stale_after_seconds: float) -> list[dict[str, Any]]:
        try:
            with self._engine.connect() as conn:
                return [dict(r._mapping) for r in conn.execute(text("""
                    SELECT worker_id, hostname, pid, state, started_at, last_seen_at, events_succeeded,
                           events_failed,
                           (state <> 'stopped' AND last_seen_at > now() - make_interval(secs => :stale)) AS healthy
                      FROM worker_heartbeats ORDER BY last_seen_at DESC LIMIT 200
                """), {"stale": stale_after_seconds})]
        except SQLAlchemyError as exc:
            raise translate_db_error(exc) from exc
