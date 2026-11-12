# Customer Requirements — Meridian Capital (fictional)

Meridian Capital is a financial services firm. Relationship managers maintain
client accounts in Salesforce. Operations, KYC and risk teams work in an
internal Customer Management System (CMS). Today records are re-keyed by hand,
which causes drift and delays onboarding.

## Requirements and status

| # | Requirement | Status |
|---|---|---|
| R1 | Account changes in Salesforce reach the CMS without manual re-entry | ✅ event-driven: signed CRM webhooks → durable inbox → workers. On-demand `POST /sync/accounts/{id}` also available |
| R1a | No account change is lost if IntegrationLab restarts or a worker crashes | ✅ persisted before acknowledgement. Leases recover crashed work (bounded attempts) |
| R2 | Salesforce is the system of record for account information (name, industry, revenue, relationship manager) | ✅ |
| R3 | The CMS is the system of record for **onboarding status** and **risk classification**; sync must never overwrite them | ✅ enforced in 3 layers, tested end to end |
| R4 | Meridian's industry taxonomy differs from Salesforce's | ✅ `value_map` in `meridian.json` |
| R5 | Revenue stored as exact decimal USD | ✅ `to_decimal(places=2)` |
| R6 | Records with missing mandatory data must not be loaded, and the reason must be visible | ✅ `transform` failures with per-field issues |
| R7 | Outages in either system must be visible and recoverable | ✅ automatic retries with backoff; dead-letter after bounded attempts; operator replay; metrics |
| R7b | Operational actions must be traceable | ✅ append-only audit log (replay, mapping publish, retention), including denied attempts |
| R7a | Sync history must survive restarts and must not contain client data | ✅ PostgreSQL, identifiers and field names only |
| R8 | No duplicate customers from repeated or concurrent syncs | ✅ idempotent create + diff + PostgreSQL advisory lock across processes |
| R9 | Client data must not appear in operational logs | ✅ tested |
| R10 | Access restricted to authorized Meridian users, isolated from other tenants | ✅ OIDC + RBAC (viewer/operator/admin); tenant from token; per-tenant credentials and mappings |
| R10a | Meridian admins can adjust their own field mapping without engineering involvement | ✅ `PUT /admin/mapping` (validated, versioned, audited by user) |
| R11 | Operational dashboard for integration health | ✅ `dashboard/`: health metrics, latest 100 events, failure explanations, retry history, manual replay for operators |

## Field mapping (Meridian, v1)

| Salesforce `Account` | CMS customer | Transform | Required | Owner |
|---|---|---|---|---|
| `Id` | `external_crm_id` | strip | yes | CRM |
| `Name` | `legal_name` | collapse whitespace | yes | CRM |
| `OwnerName` | `relationship_manager` | collapse whitespace | yes | CRM |
| `Industry` | `industry_segment` | value map → `FINANCIAL_SERVICES`, `ASSET_MANAGEMENT`, `BANKING`, `INSURANCE`, else `OTHER` | no | CRM |
| `AnnualRevenue` | `annual_revenue_usd` | decimal, 2 places, ≥ 0 | no | CRM |
| `LastModifiedDate` | `crm_last_modified_at` | ISO-8601 with timezone | no | CRM |
| — | `onboarding_status` | never written by sync | — | CMS |
| — | `risk_classification` | never written by sync | — | CMS |
