// The whole dashboard: sign-in gate, 5-second polling, and the selected event.
// The API decides what this user may see and do; the UI only mirrors /me.
import { useCallback, useEffect, useState } from "react";
import { api, describeApiError, setUnauthorizedHandler } from "./api";
import { beginSignIn, completeSignIn, signOut, storedToken } from "./auth";
import { EventDetail } from "./components/EventDetail";
import { EventList } from "./components/EventList";
import { HealthSummary } from "./components/HealthSummary";
import { Header } from "./components/Header";
import type { Me, OpsMetrics, Tenant, WebhookEvent } from "./types";

const POLL_MS = 5000;

export default function App() {
  const [token, setToken] = useState<string | null>(storedToken);
  const [signInError, setSignInError] = useState<string | null>(null);
  const [callbackPending, setCallbackPending] = useState(window.location.pathname === "/callback");

  const endSession = useCallback((message: string | null) => {
    signOut();
    setToken(null);
    setSignInError(message);
  }, []);

  useEffect(() => {
    setUnauthorizedHandler(() => endSession("Your session has expired. Please sign in again."));
  }, [endSession]);

  useEffect(() => {
    if (window.location.pathname !== "/callback") return;
    completeSignIn(window.location.search)
      .then(setToken)
      .catch((e: Error) => setSignInError(e.message))
      .finally(() => {
        window.history.replaceState(null, "", "/"); // drop the code from the address bar
        setCallbackPending(false);
      });
  }, []);

  if (callbackPending) return <main className="center"><p className="muted">Signing in…</p></main>;
  if (!token) return <SignIn error={signInError} />;
  return <Dashboard token={token} onSignOut={() => endSession(null)} />;
}

function SignIn({ error }: { error: string | null }) {
  return (
    <main className="center">
      <div className="card signin">
        <h1>Integration Health</h1>
        <p className="muted">Monitor CRM synchronization and recover failed changes.</p>
        {error && <p className="error" role="alert">{error}</p>}
        <button className="primary" onClick={async () => window.location.assign(await beginSignIn())}>Sign in</button>
      </div>
    </main>
  );
}

function Dashboard({ token, onSignOut }: { token: string; onSignOut: () => void }) {
  const [me, setMe] = useState<Me | null>(null);
  const [tenant, setTenant] = useState<Tenant | null>(null);
  const [metrics, setMetrics] = useState<OpsMetrics | null>(null);
  const [metricsError, setMetricsError] = useState<string | null>(null);
  const [events, setEvents] = useState<WebhookEvent[] | null>(null);
  const [eventsError, setEventsError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const [profileError, setProfileError] = useState<string | null>(null);

  useEffect(() => {
    api.me(token).then(setMe).catch((e) => setProfileError(describeApiError(e)));
    api.tenant(token).then(setTenant).catch(() => setTenant(null));
  }, [token]);

  const refresh = useCallback(() => {
    api.metrics(token)
      .then((m) => { setMetrics(m); setMetricsError(null); })
      .catch((e) => setMetricsError(describeApiError(e)));
    api.events(token)
      .then((ev) => { setEvents(ev); setEventsError(null); })
      .catch((e) => setEventsError(describeApiError(e)));
    setTick((t) => t + 1);
  }, [token]);

  useEffect(() => {
    refresh();
    const id = window.setInterval(refresh, POLL_MS);
    return () => window.clearInterval(id);
  }, [refresh]);

  return (
    <div className="page">
      <Header me={me} tenant={tenant} onSignOut={onSignOut} />
      {profileError && <p className="error" role="alert">{profileError}</p>}
      <HealthSummary metrics={metrics} error={metricsError} />
      <div className={selectedId ? "layout with-detail" : "layout"}>
        <EventList events={events} error={eventsError} selectedId={selectedId} onSelect={setSelectedId} />
        {selectedId && me && (
          <EventDetail
            key={selectedId}
            token={token}
            eventId={selectedId}
            canReplay={me.permissions.includes("sync:run")}
            roles={me.roles}
            refreshTick={tick}
            onClose={() => setSelectedId(null)}
            onReplayed={refresh}
          />
        )}
      </div>
      <footer className="muted small">Updates every {POLL_MS / 1000} seconds.</footer>
    </div>
  );
}
