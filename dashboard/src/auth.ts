// Browser sign-in: OAuth 2.0 authorization code flow with PKCE (S256), against the
// mock IdP (reached through the same-origin /idp path). The dashboard is a public
// client: it has no secret. The access token is kept in sessionStorage (this tab
// only, cleared when the tab closes); this is a local demonstration choice.

export const CLIENT_ID = "integrationlab-dashboard";
export const IDP_BASE = "/idp";

const TOKEN_KEY = "il.access_token";
const PENDING_KEY = "il.pkce"; // { state, verifier } for the sign-in in progress

export function redirectUri(origin: string = window.location.origin): string {
  return `${origin}/callback`;
}

function base64url(bytes: Uint8Array): string {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** RFC 7636 verifier: 32 random bytes → 43 base64url characters. */
export function randomToken(): string {
  return base64url(crypto.getRandomValues(new Uint8Array(32)));
}

export async function s256(verifier: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return base64url(new Uint8Array(digest));
}

/** Builds the authorization URL and remembers state + verifier for the callback. */
export async function beginSignIn(origin: string = window.location.origin): Promise<string> {
  const state = randomToken();
  const verifier = randomToken();
  sessionStorage.setItem(PENDING_KEY, JSON.stringify({ state, verifier }));
  const params = new URLSearchParams({
    response_type: "code",
    client_id: CLIENT_ID,
    redirect_uri: redirectUri(origin),
    code_challenge: await s256(verifier),
    code_challenge_method: "S256",
    state,
  });
  return `${IDP_BASE}/authorize?${params}`;
}

export class SignInError extends Error {}

/**
 * Handles /callback?code=…&state=…: the state must match the one this tab stored
 * (otherwise the response wasn't for our request), and the pending entry is
 * removed before anything else so it can't be used twice.
 */
export async function completeSignIn(search: string, origin: string = window.location.origin): Promise<string> {
  const pendingRaw = sessionStorage.getItem(PENDING_KEY);
  sessionStorage.removeItem(PENDING_KEY);
  const params = new URLSearchParams(search);
  const code = params.get("code");
  const state = params.get("state");
  if (params.get("error")) throw new SignInError("The identity provider declined the sign-in.");
  if (!pendingRaw || !code || !state) throw new SignInError("No sign-in is in progress. Please sign in again.");
  const pending = JSON.parse(pendingRaw) as { state: string; verifier: string };
  if (state !== pending.state) throw new SignInError("The sign-in response didn't match this request. Please sign in again.");

  const response = await fetch(`${IDP_BASE}/oauth/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      code,
      code_verifier: pending.verifier,
      client_id: CLIENT_ID,
      redirect_uri: redirectUri(origin),
    }),
  });
  const body = await response.json().catch(() => null);
  if (!response.ok || !body?.access_token) throw new SignInError("Sign-in failed or expired. Please sign in again.");
  sessionStorage.setItem(TOKEN_KEY, body.access_token);
  return body.access_token as string;
}

export function storedToken(): string | null {
  return sessionStorage.getItem(TOKEN_KEY);
}

export function signOut(): void {
  sessionStorage.removeItem(TOKEN_KEY);
  sessionStorage.removeItem(PENDING_KEY);
}
