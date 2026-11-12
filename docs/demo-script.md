# Demo script (about 10 minutes)

Shows Meridian Capital how IntegrationLab keeps their CRM and customer system in
sync, what happens when the customer system goes down, and how an operator
recovers. Everything runs locally against the mock CRM, CMS and identity provider.

## Before the demo

```bash
docker compose up -d --build --wait     # includes the dashboard on http://localhost:8080
```

Have a terminal ready with a CRM token. The CRM changes and the outage are
triggered from the terminal; they're not part of the customer-facing screen.

```bash
CRM=$(curl -s -u integrationlab-dev:dev-only-secret -d grant_type=client_credentials \
  localhost:8001/oauth/token | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
crm_change() {  # crm_change SF-1002 9100000
  curl -s -o /dev/null -w "CRM update: %{http_code}\n" -X PATCH -H "Authorization: Bearer $CRM" \
    -H 'Content-Type: application/json' -d "{\"AnnualRevenue\": $2}" localhost:8001/crm/accounts/$1
}
```

Demo users (password `dev-only-password`): `meridian-viewer`, `meridian-operator`,
`harbor-operator`.

## 1. Sign in and orient (viewer)

1. Open http://localhost:8080 and choose **Sign in**. On the identity provider's
   page, pick `meridian-viewer`.
2. Point out the header: organization, user, **Viewer** role, "Read-only access".
3. Walk through the health tiles: running sync workers, events that need
   attention, the waiting queue, syncs in the last hour, and time to sync (95th
   percentile). "Not available" means there's nothing to measure yet; it isn't zero.

## 2. A change flows through

1. In the terminal: `crm_change SF-1002 9100000`.
2. Within about 5 seconds, a new row appears under **Recent CRM changes** and
   turns **Synced**. Open it: **Synchronization result** lists the fields that
   were updated in the customer system. No customer data is shown, only field names.
3. Mention that the table shows only the latest 100 events.

## 3. The customer system goes down

1. Simulate an outage (the CMS rejects all writes):
   ```bash
   curl -s -X PUT localhost:8002/_admin/faults -H 'Content-Type: application/json' \
     -d '{"mode":"error","remaining":100,"status_code":503,"writes_only":true}'
   crm_change SF-1001 7250000
   ```
2. Watch the event go **Queued → In progress → Queued** while IntegrationLab
   retries automatically with increasing delays. After the last attempt (about
   20 s with the compose settings) it becomes **Retries exhausted**, and
   **Needs attention** goes up.
3. Open the event. Show **What went wrong** ("The customer system was
   temporarily unavailable. This is usually temporary.") and the
   **Processing attempts** timeline: every attempt, its outcome and when the next
   retry was scheduled.
4. As a viewer, the **Replay event** button is disabled: "Operators and admins
   can replay events. Your role: viewer." The API enforces this too: a viewer's
   replay request gets 403 and is recorded in the audit log.

## 4. Recover (operator)

1. Restore the customer system:
   ```bash
   curl -s -X PUT localhost:8002/_admin/faults -H 'Content-Type: application/json' -d '{"mode":"none"}'
   ```
2. **Sign out**, then sign in as `meridian-operator` ("Can inspect and replay").
3. **Needs attention** → open the SF-1001 event → **Replay event**, enter a
   reason ("Customer system back online") → **Confirm replay**.
4. The panel confirms the replay; within a few seconds the status is **Synced**,
   **Recovery status** shows the manual replay, and the timeline shows
   "Manually replayed" followed by a successful attempt. The replay, with the
   operator's name and reason, is in the audit log (`GET /admin/audit`, admins).

## 5. Tenant isolation

Sign out and sign in as `harbor-operator`. Harborview Trust's dashboard shows
only its own events. None of Meridian's accounts appear, and requesting a
Meridian event by ID returns "not found". The organization always comes from
the signed-in user's token; the browser can't choose it.

## Afterwards

Make sure the outage is cleared (step 4.1). Nothing else needs resetting: the
demo only adds events, attempts and audit entries.
