"""Retention policy with a safe cleanup mechanism.

Eligible for deletion (and nothing else):
- ``webhook_events`` with status ``succeeded`` processed more than
  ``RETENTION_SUCCEEDED_EVENT_DAYS`` ago (attempt rows cascade with them);
- ``webhook_events`` with status ``ignored`` received more than
  ``RETENTION_IGNORED_EVENT_DAYS`` ago;
- ``sync_records`` finished more than ``RETENTION_SYNC_RECORD_DAYS`` ago that no
  webhook event still references.

Never deleted: pending, processing, failed or dead-lettered events (they may
still need action), the audit log (append-only), tenants, integrations and
mapping versions.

Safety:
- the default is a **dry run** that only counts;
- deleting requires ``RETENTION_ENABLED=true`` **and** an explicit ``execute``;
- deletes run in bounded batches, each in its own short transaction, using
  ``FOR UPDATE SKIP LOCKED`` so they never block workers;
- every executed purge writes an audit record per tenant with the counts.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import Engine, text

from app.core.config import RetentionSettings
from app.repositories.audit import AuditEntry, emit_log, insert_audit


class RetentionDisabled(RuntimeError):
    pass


@dataclass
class RetentionReport:
    executed: bool
    #: category -> tenant -> rows
    counts: dict[str, dict[str, int]] = field(default_factory=dict)

    def total(self) -> int:
        return sum(n for per_tenant in self.counts.values() for n in per_tenant.values())


_CATEGORIES = {
    "succeeded_events": ("webhook_events",
                         "status = 'succeeded' AND processed_at < now() - make_interval(days => :days)",
                         "succeeded_event_days"),
    "ignored_events": ("webhook_events",
                       "status = 'ignored' AND received_at < now() - make_interval(days => :days)",
                       "ignored_event_days"),
    "sync_records": ("sync_records",
                     "finished_at < now() - make_interval(days => :days) "
                     "AND NOT EXISTS (SELECT 1 FROM webhook_events w WHERE w.sync_record_id = sync_records.id)",
                     "sync_record_days"),
}


def plan(engine: Engine, settings: RetentionSettings) -> RetentionReport:
    report = RetentionReport(executed=False)
    with engine.connect() as conn:
        for name, (table, where, days_attr) in _CATEGORIES.items():
            rows = conn.execute(text(f"SELECT tenant_id, count(*) FROM {table} WHERE {where} GROUP BY tenant_id"),
                                {"days": getattr(settings, days_attr)}).all()
            report.counts[name] = {t: n for t, n in rows}
    return report


def execute(engine: Engine, settings: RetentionSettings, *, actor: str) -> RetentionReport:
    if not settings.enabled:
        raise RetentionDisabled("retention cleanup is disabled (set RETENTION_ENABLED=true to allow deletion)")
    report = RetentionReport(executed=True)
    for name, (table, where, days_attr) in _CATEGORIES.items():
        per_tenant: dict[str, int] = {}
        params = {"days": getattr(settings, days_attr), "batch": settings.batch_size}
        while True:
            with engine.begin() as conn:  # one short transaction per batch
                rows = conn.execute(text(
                    f"DELETE FROM {table} WHERE id IN (SELECT id FROM {table} WHERE {where} "
                    f"LIMIT :batch FOR UPDATE SKIP LOCKED) RETURNING tenant_id"), params).all()
            for (tenant,) in rows:
                per_tenant[tenant] = per_tenant.get(tenant, 0) + 1
            if len(rows) < settings.batch_size:
                break
        report.counts[name] = per_tenant

    tenants = {t for per in report.counts.values() for t in per}
    with engine.begin() as conn:
        for tenant in sorted(tenants):
            entry = AuditEntry(actor=actor, action="retention.purge", outcome="success", tenant_id=tenant,
                               target_type="retention",
                               details={name: per.get(tenant, 0) for name, per in report.counts.items()} | {
                                   "succeeded_event_days": settings.succeeded_event_days,
                                   "ignored_event_days": settings.ignored_event_days,
                                   "sync_record_days": settings.sync_record_days})
            insert_audit(conn, entry)
            emit_log(entry)
    return report
