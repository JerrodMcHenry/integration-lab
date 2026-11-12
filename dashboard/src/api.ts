// Typed client for the IntegrationLab API. The API is the only authority for
// authentication, permissions and tenant isolation; the browser just sends the
// bearer token. All calls go to same-origin /api (forwarded by Vite or nginx).
import type { Attempt, Me, OpsMetrics, ReplayResult, SyncRecord, Tenant, WebhookEvent } from "./types";

export const API_BASE = "/api";
/** The API's own cap for one page of events (GET /webhooks/events?limit=). */
export const EVENT_LIMIT = 100;

export class ApiError extends Error {
  constructor(public status: number, public detail: string) {
    super(detail);
  }
}

let onUnauthorized: () => void = () => {};
/** Called on any 401 so the app can return to sign-in (expired or invalid token). */
export function setUnauthorizedHandler(handler: () => void): void {
  onUnauthorized = handler;
}

function detailOf(body: unknown, status: number): string {
  if (body && typeof body === "object" && "detail" in body) {
    const d = (body as { detail: unknown }).detail;
    if (typeof d === "string") return d;
  }
  return `Request failed (HTTP ${status})`;
}

export async function request<T>(token: string, path: string, init: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: { Accept: "application/json", Authorization: `Bearer ${token}`, ...(init.headers ?? {}) },
    });
  } catch {
    throw new ApiError(0, "The IntegrationLab API could not be reached.");
  }
  const body = await response.json().catch(() => null);
  if (response.status === 401) onUnauthorized();
  if (!response.ok) throw new ApiError(response.status, detailOf(body, response.status));
  return body as T;
}

export const api = {
  me: (t: string) => request<Me>(t, "/me"),
  tenant: (t: string) => request<Tenant>(t, "/tenant"),
  metrics: (t: string) => request<OpsMetrics>(t, "/ops/metrics"),
  events: (t: string) => request<WebhookEvent[]>(t, `/webhooks/events?limit=${EVENT_LIMIT}`),
  event: (t: string, id: string) => request<WebhookEvent>(t, `/webhooks/events/${encodeURIComponent(id)}`),
  attempts: (t: string, id: string) => request<Attempt[]>(t, `/webhooks/events/${encodeURIComponent(id)}/attempts`),
  syncRecord: (t: string, id: string) => request<SyncRecord>(t, `/sync/history/${encodeURIComponent(id)}`),
  replay: (t: string, id: string, reason: string) =>
    request<ReplayResult>(t, `/webhooks/events/${encodeURIComponent(id)}/replay`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(reason.trim() ? { reason: reason.trim() } : {}),
    }),
};

/** Customer-facing sentence for a failed API call; the server's detail is kept for technical context. */
export function describeApiError(err: unknown): string {
  if (!(err instanceof ApiError)) return "Something went wrong. Please try again.";
  switch (err.status) {
    case 0:
      return "IntegrationLab can't be reached. Check your connection and try again.";
    case 401:
      return "Your session has expired. Please sign in again.";
    case 403:
      return "Your role doesn't allow this action.";
    case 404:
      return "This item no longer exists or isn't visible to your organization.";
    case 409:
      return "This event can't be replayed in its current state. It may already have been replayed.";
    case 503:
      return "IntegrationLab is temporarily unavailable. Please try again shortly.";
    default:
      return `The request failed (HTTP ${err.status}).`;
  }
}
