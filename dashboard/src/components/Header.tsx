import type { Me, Tenant } from "../types";

export function Header({ me, tenant, onSignOut }: { me: Me | null; tenant: Tenant | null; onSignOut: () => void }) {
  const canRecover = me?.permissions.includes("sync:run") ?? false;
  return (
    <header className="header">
      <div>
        <h1>Integration Health</h1>
        <p className="muted">CRM → customer system synchronization{tenant ? ` · ${tenant.display_name}` : ""}</p>
      </div>
      {me && (
        <div className="who">
          <span>{me.subject}</span>
          {me.roles.length ? (
            me.roles.map((r) => (
              <span key={r} className={`role role-${r}`}>{r}</span>
            ))
          ) : (
            <span className="role">no role</span>
          )}
          <span className="muted small">{canRecover ? "Can inspect and replay" : "Read-only access"}</span>
          <button className="link" onClick={onSignOut}>Sign out</button>
        </div>
      )}
    </header>
  );
}
