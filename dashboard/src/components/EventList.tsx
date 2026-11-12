import { useState } from "react";
import { EVENT_LIMIT } from "../api";
import { EVENT_STATUS, formatRelative } from "../format";
import type { EventStatus, WebhookEvent } from "../types";
import { StatusBadge } from "./StatusBadge";

const FILTERS: { key: string; label: string; statuses: EventStatus[] | null }[] = [
  { key: "all", label: "All", statuses: null },
  { key: "attention", label: "Needs attention", statuses: ["failed", "dead_letter"] },
  { key: "waiting", label: "Waiting / retrying", statuses: ["pending", "processing"] },
  { key: "succeeded", label: "Synced", statuses: ["succeeded"] },
];

interface Props {
  events: WebhookEvent[] | null;
  error: string | null;
  selectedId: string | null;
  onSelect: (id: string) => void;
}

export function EventList({ events, error, selectedId, onSelect }: Props) {
  const [filter, setFilter] = useState("all");
  const active = FILTERS.find((f) => f.key === filter)!;
  const rows = (events ?? []).filter((e) => !active.statuses || active.statuses.includes(e.status));

  return (
    <section className="panel events" aria-label="Recent CRM events">
      <div className="panel-head">
        <h2>Recent CRM changes</h2>
        <p className="muted small">
          Shows the latest {EVENT_LIMIT} events only{events ? ` (${events.length} loaded)` : ""}. Older events aren't listed.
        </p>
      </div>
      <div className="filters" role="tablist" aria-label="Filter events">
        {FILTERS.map((f) => {
          const count = events?.filter((e) => !f.statuses || f.statuses.includes(e.status)).length;
          return (
            <button key={f.key} role="tab" aria-selected={filter === f.key}
                    className={filter === f.key ? "filter active" : "filter"} onClick={() => setFilter(f.key)}>
              {f.label}{count !== undefined ? ` (${count})` : ""}
            </button>
          );
        })}
      </div>
      {error && <p className="error" role="alert">{error}</p>}
      {events === null && !error && <p className="muted">Loading events…</p>}
      {events !== null && rows.length === 0 && <p className="muted empty">No events in this view.</p>}
      {rows.length > 0 && (
        <table>
          <thead>
            <tr><th>Status</th><th>Account</th><th>Change</th><th>Received</th><th>Attempts</th></tr>
          </thead>
          <tbody>
            {rows.map((e) => (
              <tr key={e.id} className={e.id === selectedId ? "selected" : ""} onClick={() => onSelect(e.id)}
                  onKeyDown={(k) => (k.key === "Enter" || k.key === " ") && onSelect(e.id)} tabIndex={0}
                  aria-selected={e.id === selectedId}>
                <td><StatusBadge {...EVENT_STATUS[e.status]} /></td>
                <td className="mono">{e.crm_account_id ?? "—"}</td>
                <td>{e.event_type}</td>
                <td title={e.received_at}>{formatRelative(e.received_at)}</td>
                <td>{e.attempts}{e.replay_count ? <span className="muted small"> · replayed {e.replay_count}×</span> : null}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
