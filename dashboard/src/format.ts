// Plain-language wording for statuses and error codes, plus date/number formatting.
// Every code here is one the backend actually produces (see docs/api-contracts.md).
import type { AttemptOutcome, EventStatus } from "./types";

export type Tone = "good" | "warn" | "bad" | "neutral";

export const EVENT_STATUS: Record<EventStatus, { label: string; tone: Tone; meaning: string }> = {
  succeeded: { label: "Synced", tone: "good", meaning: "The change was applied to the customer system." },
  pending: { label: "Queued", tone: "neutral", meaning: "Waiting to be processed." },
  processing: { label: "In progress", tone: "neutral", meaning: "A worker is applying the change now." },
  failed: {
    label: "Needs attention",
    tone: "bad",
    meaning: "This change can't succeed without a fix (for example missing or invalid data). It won't be retried automatically.",
  },
  dead_letter: {
    label: "Retries exhausted",
    tone: "bad",
    meaning: "Every automatic retry failed. Once the cause is fixed, an operator can replay it.",
  },
  ignored: { label: "Not applicable", tone: "neutral", meaning: "This event type isn't synchronized, so nothing was done." },
};

export const ATTEMPT_OUTCOME: Record<AttemptOutcome, { label: string; tone: Tone }> = {
  succeeded: { label: "Succeeded", tone: "good" },
  retry_scheduled: { label: "Failed: retry scheduled", tone: "warn" },
  failed: { label: "Failed: not retryable", tone: "bad" },
  dead_letter: { label: "Failed: retries exhausted", tone: "bad" },
  lease_expired: { label: "Interrupted: worker stopped", tone: "warn" },
};

const ERRORS: Record<string, string> = {
  crm_unavailable: "The CRM was temporarily unavailable.",
  crm_timeout: "The CRM took too long to respond.",
  crm_not_found: "This account no longer exists in the CRM.",
  crm_auth_failed: "IntegrationLab's credentials were rejected by the CRM.",
  crm_bad_request: "The CRM rejected the request.",
  crm_invalid_response: "The CRM returned data IntegrationLab couldn't read.",
  crm_error: "The CRM returned an error.",
  destination_unavailable: "The customer system was temporarily unavailable.",
  destination_timeout: "The customer system took too long to respond.",
  destination_auth_failed: "IntegrationLab's credentials were rejected by the customer system.",
  destination_not_found: "The customer record couldn't be found in the customer system.",
  destination_conflict: "The customer system reported a conflicting record.",
  destination_rejected_payload: "The customer system rejected the data sent to it.",
  destination_invalid_response: "The customer system returned data IntegrationLab couldn't read.",
  destination_error: "The customer system returned an error.",
  mapping_failed: "The CRM record is missing required data or has invalid values.",
  mapping_config_invalid: "The field mapping configuration is invalid.",
  tenant_connection_invalid: "The connection settings for this organization are incomplete or invalid.",
  tenant_secret_unavailable: "A credential for this organization isn't configured.",
  tenant_inactive: "This organization is suspended.",
  tenant_lookup_failed: "Organization settings couldn't be loaded.",
  sync_lock_timeout: "Another update to the same account was still in progress.",
  sync_lock_lost: "Processing was interrupted before writing, to avoid a conflicting update.",
  lock_error: "The account couldn't be locked for processing.",
  processing_deadline_exceeded: "Processing took too long and stopped before writing, to avoid a conflicting update.",
  database_unavailable: "IntegrationLab's database was temporarily unavailable.",
  history_not_persisted: "The change was processed but its record couldn't be saved.",
  worker_attempts_exhausted: "Processing was interrupted repeatedly (worker restarts).",
  lease_expired: "The worker processing this event stopped before finishing.",
  event_missing_account: "The event didn't identify an account.",
  worker_error: "An unexpected internal error occurred.",
  unexpected_error: "An unexpected internal error occurred.",
};

/** Customer-facing sentence for an error code; unknown codes fall back to a neutral sentence. */
export function explainError(code: string | null | undefined): string {
  if (!code) return "";
  return ERRORS[code] ?? "The integration reported an error.";
}

export function retryHint(retryable: boolean | null | undefined): string {
  if (retryable === true) return "This is usually temporary.";
  if (retryable === false) return "Retrying won't help until the cause is fixed.";
  return "";
}

const dateTime = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "medium" });
const relative = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });

export function formatDateTime(iso: string | null | undefined): string {
  return iso ? dateTime.format(new Date(iso)) : "—";
}

export function formatRelative(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "—";
  const seconds = Math.round((new Date(iso).getTime() - now) / 1000);
  const abs = Math.abs(seconds);
  if (abs < 60) return relative.format(seconds, "second");
  if (abs < 3600) return relative.format(Math.round(seconds / 60), "minute");
  if (abs < 86400) return relative.format(Math.round(seconds / 3600), "hour");
  return relative.format(Math.round(seconds / 86400), "day");
}

/** Durations from the API are seconds; null means "not measured", which is shown differently from 0. */
export function formatSeconds(value: number | null | undefined): string {
  if (value === null || value === undefined) return "Not available";
  if (value < 1) return `${Math.round(value * 1000)} ms`;
  if (value < 120) return `${value.toFixed(1)} s`;
  return `${Math.round(value / 60)} min`;
}

export const SYNC_RESULT: Record<string, string> = {
  created: "A new customer record was created.",
  updated: "The customer record was updated.",
  unchanged: "The customer record was already up to date; nothing was changed.",
  skipped_stale: "Skipped: a newer version of this account had already been synchronized.",
  failed: "The synchronization failed.",
};
