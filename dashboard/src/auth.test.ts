import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { beginSignIn, CLIENT_ID, completeSignIn, s256, SignInError, signOut, storedToken } from "./auth";

const ORIGIN = "http://localhost:5173";

function tokenResponse(ok = true) {
  return vi.fn().mockResolvedValue({
    ok,
    status: ok ? 200 : 400,
    json: () => Promise.resolve(ok ? { access_token: "tok-123" } : { error: "invalid_grant" }),
  });
}

beforeEach(() => sessionStorage.clear());
afterEach(() => vi.unstubAllGlobals());

describe("PKCE", () => {
  it("computes the RFC 7636 S256 challenge", async () => {
    // Appendix B test vector
    expect(await s256("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")).toBe("E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM");
  });

  it("builds an authorization request with S256, a fresh state and the exact redirect URI", async () => {
    const url = new URL(await beginSignIn(ORIGIN), ORIGIN);
    const p = url.searchParams;
    expect(url.pathname).toBe("/idp/authorize");
    expect(p.get("client_id")).toBe(CLIENT_ID);
    expect(p.get("redirect_uri")).toBe(`${ORIGIN}/callback`);
    expect(p.get("code_challenge_method")).toBe("S256");
    const pending = JSON.parse(sessionStorage.getItem("il.pkce")!);
    expect(p.get("state")).toBe(pending.state);
    expect(p.get("code_challenge")).toBe(await s256(pending.verifier));
    expect(url.search).not.toContain(pending.verifier); // the verifier never leaves the tab before the token call
    expect(url.search).not.toContain("secret");
  });
});

describe("callback", () => {
  async function started() {
    const url = new URL(await beginSignIn(ORIGIN), ORIGIN);
    return { state: url.searchParams.get("state")!, verifier: JSON.parse(sessionStorage.getItem("il.pkce")!).verifier };
  }

  it("exchanges the code with the stored verifier and stores the token", async () => {
    const { state, verifier } = await started();
    const fetchMock = tokenResponse();
    vi.stubGlobal("fetch", fetchMock);
    expect(await completeSignIn(`?code=abc&state=${state}`, ORIGIN)).toBe("tok-123");
    const body = fetchMock.mock.calls[0][1].body as URLSearchParams;
    expect(fetchMock.mock.calls[0][0]).toBe("/idp/oauth/token");
    expect(body.get("code_verifier")).toBe(verifier);
    expect(body.get("redirect_uri")).toBe(`${ORIGIN}/callback`);
    expect(body.has("client_secret")).toBe(false);
    expect(storedToken()).toBe("tok-123");
    expect(sessionStorage.getItem("il.pkce")).toBeNull();
  });

  it("rejects a mismatched state without calling the IdP", async () => {
    await started();
    const fetchMock = tokenResponse();
    vi.stubGlobal("fetch", fetchMock);
    await expect(completeSignIn("?code=abc&state=forged", ORIGIN)).rejects.toBeInstanceOf(SignInError);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(storedToken()).toBeNull();
  });

  it("rejects a callback when no sign-in was started (and a replayed callback)", async () => {
    const { state } = await started();
    vi.stubGlobal("fetch", tokenResponse());
    await completeSignIn(`?code=abc&state=${state}`, ORIGIN);
    signOut();
    const fetchMock = tokenResponse();
    vi.stubGlobal("fetch", fetchMock);
    await expect(completeSignIn(`?code=abc&state=${state}`, ORIGIN)).rejects.toBeInstanceOf(SignInError);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("surfaces a failed token exchange and stores nothing", async () => {
    const { state } = await started();
    vi.stubGlobal("fetch", tokenResponse(false));
    await expect(completeSignIn(`?code=abc&state=${state}`, ORIGIN)).rejects.toThrow("Sign-in failed");
    expect(storedToken()).toBeNull();
  });
});
