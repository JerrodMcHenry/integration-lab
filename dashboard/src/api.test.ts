import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, setUnauthorizedHandler } from "./api";

function respond(status: number, body: unknown) {
  return vi.fn().mockResolvedValue({ ok: status < 400, status, json: () => Promise.resolve(body) });
}

afterEach(() => {
  vi.unstubAllGlobals();
  setUnauthorizedHandler(() => {});
});

describe("api", () => {
  it("sends only the bearer token to same-origin /api and asks for the latest 100 events", async () => {
    const fetchMock = respond(200, []);
    vi.stubGlobal("fetch", fetchMock);
    await api.events("tok");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/webhooks/events?limit=100");
    expect(init.headers.Authorization).toBe("Bearer tok");
  });

  it("turns API errors into ApiError with the server's detail", async () => {
    vi.stubGlobal("fetch", respond(409, { detail: "only failed or dead_letter events can be replayed" }));
    const err = await api.replay("tok", "e1", "").catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect([err.status, err.detail]).toEqual([409, "only failed or dead_letter events can be replayed"]);
  });

  it("calls the unauthorized handler on 401", async () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    vi.stubGlobal("fetch", respond(401, { detail: "invalid token" }));
    await expect(api.me("tok")).rejects.toBeInstanceOf(ApiError);
    expect(handler).toHaveBeenCalledOnce();
  });

  it("reports an unreachable API as status 0", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("network")));
    const err = await api.metrics("tok").catch((e) => e);
    expect(err.status).toBe(0);
  });

  it("sends a trimmed replay reason, or no reason at all", async () => {
    const fetchMock = respond(202, {});
    vi.stubGlobal("fetch", fetchMock);
    await api.replay("tok", "e1", "  CMS back online  ");
    await api.replay("tok", "e1", "   ");
    expect(fetchMock.mock.calls[0][1].body).toBe('{"reason":"CMS back online"}');
    expect(fetchMock.mock.calls[1][1].body).toBe("{}");
  });
});
