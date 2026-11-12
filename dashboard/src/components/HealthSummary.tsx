import { formatRelative, formatSeconds, type Tone } from "../format";
import type { OpsMetrics } from "../types";

function Tile({ label, value, note, tone }: { label: string; value: string; note?: string; tone: Tone }) {
  return (
    <div className={`tile tile-${tone}`}>
      <div className="tile-label">{label}</div>
      <div className="tile-value">{value}</div>
      {note && <div className="tile-note">{note}</div>}
    </div>
  );
}

/** Every number here comes from GET /ops/metrics. When the request fails, tiles say so instead of showing 0. */
export function HealthSummary({ metrics, error }: { metrics: OpsMetrics | null; error: string | null }) {
  if (!metrics) {
    return (
      <section className="tiles" aria-label="Integration health">
        <div className="tile tile-neutral wide">
          <div className="tile-label">Health metrics</div>
          <div className="tile-value small">{error ? "Unavailable" : "Loading…"}</div>
          {error && <div className="tile-note">{error}</div>}
        </div>
      </section>
    );
  }
  const e = metrics.events;
  const w = metrics.workers;
  const attention = e.failed + e.dead_letter;
  const waiting = e.pending + e.processing;
  return (
    <section className="tiles" aria-label="Integration health">
      {error && <p className="stale" role="status">Couldn't refresh: {error} Showing the last values received.</p>}
      <Tile
        label="Sync workers"
        value={`${w.healthy} running`}
        note={w.last_heartbeat_at ? `Last check-in ${formatRelative(w.last_heartbeat_at)}` : "No worker has checked in"}
        tone={w.healthy > 0 ? "good" : "bad"}
      />
      <Tile
        label="Needs attention"
        value={String(attention)}
        note={attention ? `${e.dead_letter} retries exhausted · ${e.failed} not retryable` : "No failed events"}
        tone={attention ? "bad" : "good"}
      />
      <Tile
        label="Waiting"
        value={String(waiting)}
        note={
          e.retry_backlog
            ? `${e.retry_backlog} waiting for an automatic retry`
            : e.oldest_pending_s !== null
              ? `Oldest waiting ${formatSeconds(e.oldest_pending_s)}`
              : "Queue is empty"
        }
        tone={e.retry_backlog || e.expired_leases ? "warn" : "neutral"}
      />
      <Tile label="Synced in the last hour" value={String(e.succeeded_1h)} tone="neutral" />
      <Tile
        label="Time to sync (95th percentile)"
        value={formatSeconds(e.latency_p95_s)}
        note={
          e.latency_p50_s === null
            ? "No successful syncs in the last hour"
            : `Median ${formatSeconds(e.latency_p50_s)} · from CRM change received to customer system updated`
        }
        tone="neutral"
      />
    </section>
  );
}
