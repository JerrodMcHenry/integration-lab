import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Attempt, WebhookEvent } from "../types";
import { EventDetail } from "./EventDetail";

const DEAD: WebhookEvent = {
  id: "e1", event_id: "evt-1", event_type: "account.updated", crm_account_id: "SF-1002", status: "dead_letter",
  attempts: 5, occurred_at: null, received_at: "2026-11-12T10:00:00Z", processed_at: null,
  last_error: { code: "destination_unavailable", message: "upstream returned 503", retryable: true },
  sync_record_id: null, available_at: null, dead_lettered_at: "2026-11-12T10:05:00Z", replay_count: 0, last_replayed_at: null,
};
const ATTEMPTS: Attempt[] = [
  { attempt: 1, worker_id: "w1", started_at: null, finished_at: "2026-11-12T10:00:01Z", outcome: "retry_scheduled",
    error_code: "destination_unavailable", error_message: "x", retryable: true, next_attempt_at: "2026-11-12T10:00:05Z", sync_record_id: null },
];

/** Routes fetch by URL and records every call. */
function fakeApi(replayStatus = 202, event: WebhookEvent = DEAD) {
  let current = event;
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const reply = (status: number, body: unknown) => ({ ok: status < 400, status, json: async () => body });
    if (url.endsWith("/replay") && init?.method === "POST") {
      if (replayStatus === 202) current = { ...current, status: "pending", replay_count: current.replay_count + 1 };
      return reply(replayStatus, replayStatus === 409 ? { detail: "not replayable" } : {});
    }
    if (url.endsWith("/attempts")) return reply(200, ATTEMPTS);
    return reply(200, current);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** Like App: a replay triggers a refresh, which re-fetches the event. */
function Harness({ canReplay, roles, onReplayed }: { canReplay: boolean; roles: string[]; onReplayed: () => void }) {
  const [tick, setTick] = useState(0);
  return <EventDetail token="tok" eventId="e1" canReplay={canReplay} roles={roles} refreshTick={tick}
                      onClose={() => {}} onReplayed={() => { onReplayed(); setTick((t) => t + 1); }} />;
}

function show(canReplay: boolean, roles: string[], onReplayed = vi.fn()) {
  render(<Harness canReplay={canReplay} roles={roles} onReplayed={onReplayed} />);
  return onReplayed;
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("EventDetail", () => {
  it("explains the failure in plain language and shows the retry history", async () => {
    fakeApi();
    show(true, ["operator"]);
    expect(await screen.findByText(/customer system was temporarily unavailable\. This is usually temporary/)).toBeTruthy();
    expect(screen.getAllByText("Retries exhausted").length).toBeGreaterThan(0);
    expect(screen.getByText("Attempt 1")).toBeTruthy();
    expect(screen.getByText("Failed: retry scheduled")).toBeTruthy();
  });

  it("viewer: replay is disabled with an explanation and never sent", async () => {
    const fetchMock = fakeApi();
    show(false, ["viewer"]);
    const button = await screen.findByRole("button", { name: "Replay event" });
    expect((button as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/Operators and admins can replay events\. Your role: viewer/)).toBeTruthy();
    fireEvent.click(button);
    expect(fetchMock.mock.calls.some(([u]) => String(u).endsWith("/replay"))).toBe(false);
  });

  it("operator: confirms, sends the reason and reports success", async () => {
    const fetchMock = fakeApi();
    const onReplayed = show(true, ["operator"]);
    fireEvent.click(await screen.findByRole("button", { name: "Replay event" }));
    fireEvent.change(screen.getByLabelText("Reason (optional)"), { target: { value: "CMS restored" } });
    fireEvent.click(screen.getByRole("button", { name: "Confirm replay" }));
    await screen.findByText("Queued"); // the refreshed event is pending again...
    expect(screen.getByText(/Replay queued/)).toBeTruthy(); // ...and the confirmation is still shown
    const call = fetchMock.mock.calls.find(([u]) => String(u).endsWith("/replay"))!;
    expect(call[0]).toBe("/api/webhooks/events/e1/replay");
    expect(call[1]!.body).toBe('{"reason":"CMS restored"}');
    expect(onReplayed).toHaveBeenCalled();
  });

  it("operator: a 409 from the API is explained, not hidden", async () => {
    fakeApi(409);
    const onReplayed = show(true, ["operator"]);
    fireEvent.click(await screen.findByRole("button", { name: "Replay event" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm replay" }));
    expect(await screen.findByText(/can't be replayed in its current state/)).toBeTruthy();
    expect(onReplayed).not.toHaveBeenCalled();
  });

  it("marks where a manual replay restarted the attempt count", async () => {
    const after: Attempt = { ...ATTEMPTS[0], attempt: 1, outcome: "succeeded", error_code: null, next_attempt_at: null };
    const fetchMock = fakeApi(202, { ...DEAD, status: "succeeded", last_error: null, replay_count: 1 });
    fetchMock.mockImplementation(async (url: string) => ({
      ok: true, status: 200,
      json: async () => (url.endsWith("/attempts") ? [ATTEMPTS[0], after] : { ...DEAD, status: "succeeded", last_error: null, replay_count: 1 }),
    }));
    show(true, ["operator"]);
    expect(await screen.findByText("Manually replayed")).toBeTruthy();
    expect(screen.getAllByText("Attempt 1")).toHaveLength(2);
  });

  it("offers no replay for events that succeeded", async () => {
    fakeApi(202, { ...DEAD, status: "succeeded", last_error: null, dead_lettered_at: null });
    show(true, ["operator"]);
    await screen.findByText("The change was applied to the customer system.");
    await waitFor(() => expect(screen.queryByRole("button", { name: "Replay event" })).toBeNull());
  });
});
