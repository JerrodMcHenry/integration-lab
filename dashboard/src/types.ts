// Response shapes of the IntegrationLab API. Field for field, these mirror the
// backend models (app/api/*.py, app/schemas/sync.py). Do not add fields the API
// doesn't return.

export type Permission = "tenant:read" | "history:read" | "sync:run" | "config:read" | "config:write";

export interface Me {
  subject: string;
  tenant_id: string;
  roles: string[];
  permissions: Permission[];
}

export interface Tenant {
  id: string;
  display_name: string;
  status: string;
}

export interface EventMetrics {
  pending: number;
  ready: number;
  retry_backlog: number;
  processing: number;
  expired_leases: number;
  failed: number;
  dead_letter: number;
  succeeded_1h: number;
  // null when there's nothing to measure (e.g. no successes in the last hour)
  oldest_pending_s: number | null;
  latency_p50_s: number | null;
  latency_p95_s: number | null;
  latency_max_s: number | null;
}

export interface OpsMetrics {
  tenant_id: string;
  events: EventMetrics;
  workers: { healthy: number; total_seen: number; last_heartbeat_at: string | null };
}

export type EventStatus = "pending" | "processing" | "succeeded" | "failed" | "dead_letter" | "ignored";

export interface WebhookEvent {
  id: string;
  event_id: string;
  event_type: string;
  crm_account_id: string | null;
  status: EventStatus;
  attempts: number;
  occurred_at: string | null;
  received_at: string;
  processed_at: string | null;
  last_error: { code: string; message: string | null; retryable: boolean | null } | null;
  sync_record_id: string | null;
  available_at: string | null;
  dead_lettered_at: string | null;
  replay_count: number;
  last_replayed_at: string | null;
}

export type AttemptOutcome = "succeeded" | "retry_scheduled" | "failed" | "dead_letter" | "lease_expired";

export interface Attempt {
  attempt: number;
  worker_id: string | null;
  started_at: string | null;
  finished_at: string;
  outcome: AttemptOutcome;
  error_code: string | null;
  error_message: string | null;
  retryable: boolean | null;
  next_attempt_at: string | null;
  sync_record_id: string | null;
}

export interface SyncRecord {
  id: string;
  tenant_id: string;
  crm_account_id: string;
  status: "created" | "updated" | "unchanged" | "skipped_stale" | "failed";
  stage: string | null;
  error: { code: string; message: string; retryable: boolean; upstream_status: number | null } | null;
  destination_customer_id: string | null;
  fields_changed: string[];
  mapping_version: number | null;
  source_last_modified: string | null;
  started_at: string;
  finished_at: string;
  duration_ms: number;
}

export interface ReplayResult {
  status: "replayed";
  id: string;
  previous_status: EventStatus;
  new_status: "pending";
}
