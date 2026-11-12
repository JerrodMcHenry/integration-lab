# Engineering rules for IntegrationLab

These apply to every change, human or AI. Start with `docs/codebase-guide.md`.

## Before writing code
- Find the module that already owns the concern and extend it. Reuse before creating.
- **Before adding a module, class, Protocol or package**, state in your summary:
  what problem it solves, why an existing component can't reasonably handle it,
  and what it costs. If you can't answer, don't add it.
- No speculative infrastructure: no caches, plugin systems, generic utilities,
  feature flags or fallback modes without a current requirement and a test that needs it.

## Structure
- Path of a request: route (`app/api/`) → dependency (`app/api/dependencies.py`) → service
  (`app/services/`) → repository (`app/repositories/`) or client (`app/integrations/`).
- Long-lived objects are constructed only in `app/wiring.py`. Each FastAPI dependency is defined
  once, in `app/api/dependencies.py`.
- Keep routes thin. Business rules live in services. SQL lives in repositories.
- Only `app/api/` and `app/main.py` import FastAPI. The worker never imports `app.api`.
- Don't add function-level imports to break import cycles. Fix the layering instead.
- Prefer a function to a class when there is no state. One implementation means no
  Protocol, unless a test fake is the second implementation and is actually used.
- Explicit data flow: pass values in; name things for what they do.
- Tests: `conftest.py` files contain fixtures only. Shared helpers go in `tests/support/`.
  Test modules never import from other test modules.
- Tests never block without a bound: use `tests/support/concurrency.py` (failing, with diagnostics,
  on timeout) and always clean up threads and processes in `finally`.

## Dependencies and data
- No new dependency without explicit approval.
- Migrations are additive. Destructive schema changes, data deletion, and resetting
  databases or Docker volumes require explicit approval.

## Invariants (never weaken without approval)
- Tenant identity comes only from verified credentials (JWT claim or webhook HMAC).
  Every repository method takes `tenant_id`.
- Secrets, tokens, signatures and customer payloads are never logged or stored in events.
- Sync writes only CRM-owned fields (`app/schemas/internal_customer.py`).
- Webhooks are persisted (committed) before they're acknowledged.
- No database transaction stays open during an external HTTP call.
- Retries are bounded. Sensitive operational actions are audited.

## Tests and docs
- Tests proportional to risk. Security, tenant isolation and concurrency changes need
  negative tests. Check that a new test fails when the line it guards is removed.
- Don't delete meaningful tests to reduce test count.
- Update the docs that describe changed behaviour in the same change. Docs describe the
  current system; history belongs in `docs/engineering-decisions.md`.
- Full suite before finishing: `REQUIRE_POSTGRES=1 pytest`.

## Change size
- Small, reviewable changes, one concern each.
- Never expand architecture silently: list added files and abstractions in your summary.
- Don't commit or push. The repository owner handles Git.
