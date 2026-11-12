import { useEffect, useState } from "react";
import { api, describeApiError } from "../api";
import {
  ATTEMPT_OUTCOME,
  EVENT_STATUS,
  explainError,
  formatDateTime,
  formatRelative,
  retryHint,
  SYNC_RESULT,
} from "../format";
import type { Attempt, SyncRecord, WebhookEvent } from "../types";
import { StatusBadge } from "./StatusBadge";

const REPLAYABLE = new Set(["failed", "dead_letter"]);

interface Props {
  token: string;
  eventId: string;
  canReplay: boolean;
  roles: string[];
  refreshTick: number; // changes on every dashboard poll
  onClose: () => void;
  onReplayed: () => void;
}

/** Rendered with key={eventId}, so selecting another event starts from fresh state. */
export function EventDetail({ token, eventId, canReplay, roles, refreshTick, onClose, onReplayed }: Props) {
  const [event, setEvent] = useState<WebhookEvent | null>(null);
  const [attempts, setAttempts] = useState<Attempt[] | null>(null);
  const [record, setRecord] = useState<SyncRecord | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Kept here, not in ReplayAction: after a replay the event is no longer replayable,
  // so ReplayAction disappears, but the confirmation must stay visible.
  const [replayed, setReplayed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    Promise.all([api.event(token, eventId), api.attempts(token, eventId)])
      .then(async ([ev, at]) => {
        const rec = ev.sync_record_id ? await api.syncRecord(token, ev.sync_record_id).catch(() => null) : null;
        if (cancelled) return;
        setEvent(ev);
        setAttempts(at);
        setRecord(rec);
        setError(null);
      })
      .catch((e) => !cancelled && setError(describeApiError(e)));
    return () => {
      cancelled = true;
    };
  }, [token, eventId, refreshTick]);

  return (
    <aside className="panel detail" aria-label="Event details">
      <div className="panel-head row">
        <h2>Event details</h2>
        <button className="link" onClick={onClose} aria-label="Close details">Close</button>
      </div>
      {error && <p className="error" role="alert">{error}</p>}
      {!event && !error && <p className="muted">Loading…</p>}
      {event && (
        <>
          <Summary event={event} />
          {event.last_error && event.status !== "succeeded" && <WhatWentWrong error={event.last_error} />}
          <Recovery event={event} />
          {replayed && (
            <p className="success block" role="status">
              Replay queued. The event will be processed again within a few seconds; its status updates here.
            </p>
          )}
          {REPLAYABLE.has(event.status) && (
            <ReplayAction key={event.id} token={token} event={event} canReplay={canReplay} roles={roles}
                          onReplayed={() => { setReplayed(true); onReplayed(); }} />
          )}
          <Timeline attempts={attempts} />
          {record && <SyncResult record={record} />}
        </>
      )}
    </aside>
  );
}

function Summary({ event }: { event: WebhookEvent }) {
  const s = EVENT_STATUS[event.status];
  return (
    <div className="block">
      <div className="row">
        <span className="mono strong">{event.crm_account_id ?? "Unknown account"}</span>
        <StatusBadge label={s.label} tone={s.tone} />
      </div>
      <p>{s.meaning}</p>
      <dl className="facts">
        <dt>Change</dt><dd>{event.event_type}</dd>
        <dt>Changed in CRM</dt><dd>{formatDateTime(event.occurred_at)}</dd>
        <dt>Received</dt><dd>{formatDateTime(event.received_at)}</dd>
        <dt>Finished</dt><dd>{formatDateTime(event.processed_at)}</dd>
        <dt>Event ID</dt><dd className="mono small">{event.event_id}</dd>
      </dl>
    </div>
  );
}

function WhatWentWrong({ error }: { error: NonNullable<WebhookEvent["last_error"]> }) {
  return (
    <div className="block callout-bad">
      <h3>What went wrong</h3>
      <p>{explainError(error.code)} {retryHint(error.retryable)}</p>
      <details>
        <summary>Technical details</summary>
        <p className="mono small">{error.code}{error.message ? `: ${error.message}` : ""}</p>
      </details>
    </div>
  );
}

function Recovery({ event }: { event: WebhookEvent }) {
  const lines: string[] = [];
  if (event.status === "pending" && event.attempts > 0 && event.available_at) {
    lines.push(`Next automatic retry ${formatRelative(event.available_at)} (${formatDateTime(event.available_at)}).`);
  }
  if (event.status === "dead_letter" && event.dead_lettered_at) {
    lines.push(`Automatic retries stopped ${formatRelative(event.dead_lettered_at)}.`);
  }
  if (event.replay_count > 0) {
    lines.push(`Manually replayed ${event.replay_count} time${event.replay_count > 1 ? "s" : ""}, most recently ${formatRelative(event.last_replayed_at)}.`);
  }
  if (!lines.length) return null;
  return (
    <div className="block">
      <h3>Recovery status</h3>
      {lines.map((l) => <p key={l}>{l}</p>)}
    </div>
  );
}

function ReplayAction({ token, event, canReplay, roles, onReplayed }: {
  token: string; event: WebhookEvent; canReplay: boolean; roles: string[]; onReplayed: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  if (!canReplay) {
    return (
      <div className="block">
        <button disabled title="Requires the operator or admin role">Replay event</button>
        <p className="muted small">
          Operators and admins can replay events. Your role: {roles.length ? roles.join(", ") : "none"}.
        </p>
      </div>
    );
  }

  async function submit() {
    setBusy(true);
    try {
      await api.replay(token, event.id, reason);
      setConfirming(false);
      onReplayed();
    } catch (e) {
      setFailure(describeApiError(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="block">
      {failure && <p className="error" role="alert">{failure}</p>}
      {!confirming ? (
        <button className="primary" onClick={() => { setConfirming(true); setFailure(null); }}>Replay event</button>
      ) : (
        <div className="confirm">
          <p>Process this CRM change again? Make sure the cause has been fixed first. The replay is recorded in the audit log.</p>
          <label htmlFor="reason">Reason (optional)</label>
          <textarea id="reason" maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)}
                    placeholder="e.g. Customer system back online" />
          <div className="row gap">
            <button className="primary" onClick={submit} disabled={busy}>{busy ? "Replaying…" : "Confirm replay"}</button>
            <button onClick={() => setConfirming(false)} disabled={busy}>Cancel</button>
          </div>
        </div>
      )}
    </div>
  );
}

function Timeline({ attempts }: { attempts: Attempt[] | null }) {
  return (
    <div className="block">
      <h3>Processing attempts</h3>
      {attempts === null && <p className="muted">Loading…</p>}
      {attempts?.length === 0 && <p className="muted">Not processed yet.</p>}
      {attempts && attempts.length > 0 && (
        <ol className="timeline">
          {attempts.flatMap((a, i) => {
            const o = ATTEMPT_OUTCOME[a.outcome];
            // A manual replay resets the attempt counter, so a lower number marks where it happened.
            const replayedBefore = i > 0 && a.attempt <= attempts[i - 1].attempt;
            const item = (
              <li key={i} className={`tl-${o.tone}`}>
                <div className="row">
                  <strong>Attempt {a.attempt}</strong>
                  <StatusBadge label={o.label} tone={o.tone} />
                </div>
                <div className="muted small">{formatDateTime(a.finished_at)}</div>
                {a.error_code && <p>{explainError(a.error_code)} <span className="mono small">({a.error_code})</span></p>}
                {a.next_attempt_at && <p className="small">Retry scheduled for {formatDateTime(a.next_attempt_at)}</p>}
              </li>
            );
            return replayedBefore
              ? [<li key={`replay-${i}`} className="tl-replay"><strong>Manually replayed</strong></li>, item]
              : [item];
          })}
        </ol>
      )}
    </div>
  );
}

function SyncResult({ record }: { record: SyncRecord }) {
  return (
    <div className="block">
      <h3>Synchronization result</h3>
      <p>{SYNC_RESULT[record.status] ?? record.status}</p>
      <dl className="facts">
        {record.fields_changed.length > 0 && (<><dt>Fields updated</dt><dd>{record.fields_changed.join(", ")}</dd></>)}
        {record.destination_customer_id && (<><dt>Customer record</dt><dd className="mono">{record.destination_customer_id}</dd></>)}
        {record.mapping_version !== null && (<><dt>Mapping version</dt><dd>{record.mapping_version}</dd></>)}
        <dt>Duration</dt><dd>{record.duration_ms} ms</dd>
      </dl>
    </div>
  );
}
