# IntegrationLab

### Enterprise Integration Platform | Python · FastAPI · PostgreSQL · React · TypeScript

**A multi-tenant, event-driven integration platform that synchronizes customer data between a Salesforce-style CRM and an internal financial-services system.**

IntegrationLab demonstrates how enterprise integrations handle secure authentication, customer-specific data mapping, reliable event processing, system failures, and operational recovery.

The application includes a working React dashboard, REST API, PostgreSQL persistence, background workers, and simulated enterprise systems.

**[Architecture](docs/architecture.md) · [Live Demo Guide](docs/demo-script.md) · [Engineering Decisions](docs/engineering-decisions.md) · [API Documentation](docs/api-contracts.md)**

---

## The Business Problem

Financial-services organizations often maintain customer information across multiple disconnected systems.

For this project, **Meridian Capital** is a fictional financial-services firm whose relationship managers use Salesforce to maintain customer accounts, while operations, compliance, and risk teams work in an internal Customer Management System (CMS).

Without an integration, employees must manually transfer updates between systems.

This creates several operational challenges:

- **Data inconsistency:** Customer information can differ across systems.
- **Manual work:** Teams spend time re-entering information.
- **Operational delays:** Outdated records can slow onboarding and servicing.
- **Limited visibility:** Failed updates may go unnoticed.
- **Recovery challenges:** Operations teams need a reliable way to investigate and correct synchronization failures.

### The Solution

IntegrationLab provides a secure, automated connection between these systems.

When a CRM account changes, the platform receives a webhook, validates the request, persists the event, and processes the update asynchronously.

Customer-specific mapping rules transform the source data into the destination format while protecting fields owned by the internal system.

If synchronization fails, IntegrationLab records the error, retries when appropriate, and gives authorized operators the ability to investigate and recover the event.

The result is a working demonstration of an enterprise integration platform designed around **reliability, security, observability, and operational control**.

---

## Integration Operations Dashboard

IntegrationLab includes a React and TypeScript dashboard for monitoring integrations and investigating synchronization failures.

The dashboard provides:

- **Integration health:** Worker availability, pending events, failures, and synchronization latency.
- **Event monitoring:** Recent CRM changes and their processing status.
- **Failure investigation:** Plain-language explanations, technical error details, and processing attempts.
- **Manual recovery:** Authorized operators can replay failed events.
- **Role-based access:** Viewers can inspect integration activity without performing recovery actions.
- **Tenant isolation:** Each customer sees only its own integration events and records.

The dashboard refreshes every five seconds and displays the latest 100 events.

### Dashboard Preview

_Add a screenshot of the running dashboard here._

Recommended location: `docs/images/integration-dashboard.png`

```md
![IntegrationLab integration operations dashboard](docs/images/integration-dashboard.png)
```

### Demonstration: Recovering a Failed Integration

A central use case is recovering from a destination-system outage.

1. A customer account is updated in the CRM.
2. The CRM sends a signed webhook to IntegrationLab.
3. IntegrationLab validates and stores the event.
4. A background worker attempts to synchronize the account.
5. The destination system becomes unavailable and returns an HTTP 503 error.
6. IntegrationLab automatically retries the operation using exponential backoff.
7. After exhausting its retry budget, the event is marked as requiring intervention.
8. An authorized operator investigates the failure in the dashboard.
9. Once the destination system recovers, the operator manually replays the event.
10. IntegrationLab successfully synchronizes the customer record and records the recovery action.

This workflow has been exercised against the running application using the local CRM, CMS, identity provider, API, database, workers, and dashboard.

See the [complete demonstration walkthrough](docs/demo-script.md).

---

## System Architecture

IntegrationLab uses an event-driven architecture with asynchronous background processing and PostgreSQL-backed persistence.

```text
                    ┌─────────────────────────┐
                    │   Salesforce-style CRM  │
                    │      (Mock Service)     │
                    └────────────┬────────────┘
                                 │
                          Signed Webhook
                                 │
                                 ▼
                    ┌─────────────────────────┐
                    │       FastAPI API       │
                    │                         │
                    │  Signature Validation   │
                    │  Authentication & RBAC  │
                    │  Tenant Isolation       │
                    └────────────┬────────────┘
                                 │
                          Persist Event
                                 │
                                 ▼
                    ┌─────────────────────────┐
                    │       PostgreSQL        │
                    │                         │
                    │  Webhook Inbox          │
                    │  Tenant Configuration   │
                    │  Sync History           │
                    │  Attempts & Audit       │
                    └────────────┬────────────┘
                                 │
                           Claim Event
                                 │
                                 ▼
                    ┌─────────────────────────┐
                    │    Background Workers   │
                    │                         │
                    │  Read CRM Account       │
                    │  Apply Field Mapping    │
                    │  Enforce Field Ownership│
                    │  Synchronize Customer   │
                    └────────────┬────────────┘
                                 │
                                 ▼
                    ┌─────────────────────────┐
                    │   Internal Customer     │
                    │   Management System     │
                    │      (Mock Service)     │
                    └─────────────────────────┘


    React Dashboard
           │
           ▼
    nginx Reverse Proxy
           │
           ▼
    FastAPI REST API
           │
           ▼
    PostgreSQL / Integration Operations
```

The architecture separates API request handling from background synchronization.

Webhook events are committed to PostgreSQL before acknowledgment, allowing processing to continue independently of the original HTTP request.

Workers claim eligible events using PostgreSQL concurrency controls, while the dashboard exposes the resulting operational state.

For implementation details, see [Architecture](docs/architecture.md) and [Codebase Guide](docs/codebase-guide.md).

---

## Key Engineering Capabilities

### 1. Reliable Webhook Processing

IntegrationLab receives HMAC-signed CRM webhooks and verifies their authenticity before accepting them.

The platform provides:

- HMAC signature verification.
- A five-minute timestamp replay window.
- Durable PostgreSQL event storage.
- Tenant-scoped event deduplication.
- Background processing independent of webhook acknowledgment.
- Worker leases and crash recovery.
- Concurrent event claiming using `FOR UPDATE SKIP LOCKED`.

**Design rationale:** An integration should not lose a customer update simply because a worker crashes or a destination system becomes temporarily unavailable.

### 2. Automatic Retries and Operational Recovery

Transient failures are handled through bounded retries with exponential backoff and jitter.

The platform distinguishes between retryable failures, such as timeouts and server errors, and non-retryable failures, such as invalid data.

When automatic recovery is exhausted, operators can inspect the event and initiate a controlled replay.

Recovery actions include:

- Recorded processing attempts.
- Error classification and retryability.
- Retry scheduling.
- Dead-letter handling.
- Manual replay with a required reason.
- Audit records for replay actions and authorization denials.

**Design rationale:** Enterprise integrations need a recovery process that is observable and controlled, rather than relying on silent retries or manual database intervention.

### 3. Multi-Tenant Architecture

IntegrationLab supports multiple customer organizations within the same application.

Each tenant has its own:

- CRM and CMS connections.
- Integration credentials and secret references.
- Field mappings.
- Webhook events.
- Synchronization history.
- Operational data.

Tenant identity is derived from authenticated access tokens or the tenant-specific webhook authentication mechanism.

Repository operations are tenant-scoped, and unauthorized cross-tenant resource requests return HTTP 404.

The demonstration includes two fictional customers:

- **Meridian Capital**
- **Harborview Trust**

**Design rationale:** A shared integration platform must enforce customer data boundaries throughout the application, not merely hide records in the frontend.

### 4. Customer-Specific Data Mapping

Different customer systems rarely use identical data models.

IntegrationLab supports validated, versioned JSON mappings that transform CRM fields into the destination system's expected structure.

The synchronization process also enforces field ownership.

For example:

| CRM-owned fields                | CMS-owned fields                 |
| ------------------------------- | -------------------------------- |
| Customer account information    | Onboarding status                |
| CRM-managed customer attributes | Risk classification              |
| Mapped source updates           | Internal operational information |

CRM synchronization updates only the fields it is authorized to manage.

The system also detects unchanged records and avoids unnecessary writes.

**Design rationale:** Integrations must respect the source of truth for each field and avoid overwriting data owned by another business system.

### 5. Authentication and Authorization

IntegrationLab implements OAuth 2.0 and OpenID Connect concepts across its integration services and user-facing dashboard.

Security capabilities include:

- JWT access-token verification using RS256.
- Authorization Code Flow with PKCE for browser sign-in.
- OAuth 2.0 client credentials for CRM access.
- API-key authentication for the mock CMS.
- Role-based access control.
- Tenant-scoped authorization.
- Audit logging for sensitive recovery actions.
- Production HTTPS validation for configured tenant endpoints.

Three application roles are supported:

| Role     | Capabilities                                                         |
| -------- | -------------------------------------------------------------------- |
| Viewer   | Inspect tenant information, integration health, events, and history  |
| Operator | Viewer permissions plus synchronization and manual replay            |
| Admin    | Operator permissions plus integration configuration and audit access |

The React dashboard reflects user permissions, but authorization is enforced by the backend API.

**Design rationale:** Frontend controls improve usability; they do not replace server-side security enforcement.

### 6. Concurrency and Idempotency

The application uses PostgreSQL concurrency mechanisms to coordinate synchronization across multiple workers.

Important controls include:

- `FOR UPDATE SKIP LOCKED` for concurrent event claiming.
- Worker leases and claim tokens.
- Advisory locks scoped to tenant and customer account.
- Pre-write ownership checks.
- Idempotent synchronization behavior.
- Detection of unchanged destination records.

**Design rationale:** Duplicate webhook deliveries and concurrent workers should not result in conflicting customer updates.

### 7. Operational Visibility

IntegrationLab records operational information that helps teams investigate integration behavior.

Available information includes:

- Event status and processing history.
- Retry attempts and error classifications.
- Worker health.
- Pending and failed event counts.
- Synchronization latency.
- Tenant-specific operational metrics.
- Prometheus-compatible platform metrics.
- Append-only audit records.

The dashboard translates technical failures into explanations understandable to customer-facing operations teams, while retaining technical details for troubleshooting.

---

## Technology Stack

| Layer                 | Technologies                                         |
| --------------------- | ---------------------------------------------------- |
| Backend               | Python 3.14, FastAPI, Pydantic v2, Uvicorn           |
| Database              | PostgreSQL 18, SQLAlchemy 2.1, Alembic, psycopg 3    |
| Frontend              | React 19, TypeScript, Vite, CSS                      |
| Authentication        | OAuth 2.0, OpenID Connect concepts, JWT, PKCE, PyJWT |
| Integrations          | REST APIs, webhooks, httpx                           |
| Background processing | Python workers, PostgreSQL concurrency controls      |
| Infrastructure        | Docker, Docker Compose, nginx                        |
| Backend testing       | pytest                                               |
| Frontend testing      | Vitest, React Testing Library, jsdom                 |

The mock CRM, CMS, and identity provider are implemented as local FastAPI services.

---

## Getting Started

### Prerequisites

- Docker Desktop
- Docker Compose
- Git

### 1. Clone the Repository

```bash
git clone <repository-url>
cd <repository-directory>
```

Replace the placeholders with the repository's actual URL and directory name.

### 2. Configure the Environment

```bash
cp .env.example .env
```

Set `POSTGRES_PASSWORD` in `.env`.

You can generate a password using:

```bash
openssl rand -hex 24
```

The example environment includes development-only credentials for the local mock services.

### 3. Start IntegrationLab

```bash
docker compose up -d --build --wait
```

Docker Compose starts the database, migration and seed process, mock integration services, identity provider, FastAPI application, background workers, and dashboard.

### 4. Open the Dashboard

**http://localhost:8080**

Sign in using one of the development accounts below.

### Demo Accounts

| Username            | Password            | Role                    |
| ------------------- | ------------------- | ----------------------- |
| `meridian-viewer`   | `dev-only-password` | Viewer                  |
| `meridian-operator` | `dev-only-password` | Operator                |
| `meridian-admin`    | `dev-only-password` | Admin                   |
| `harbor-operator`   | `dev-only-password` | Operator, second tenant |
| `harbor-admin`      | `dev-only-password` | Admin, second tenant    |

**These credentials are exclusively for the local mock environment. Never use them in a real deployment.**

### 5. Explore the API

FastAPI automatically generates interactive OpenAPI documentation:

**http://localhost:8000/docs**

### 6. Stop the Application

```bash
docker compose stop
```

This stops the containers without deleting the PostgreSQL volume.

---

## Running Tests

### Backend

Start PostgreSQL:

```bash
docker compose up -d --wait db
```

Run unit tests:

```bash
pytest -m "not postgres"
```

Run the complete PostgreSQL-backed test suite:

```bash
REQUIRE_POSTGRES=1 pytest
```

The test configuration requires a dedicated PostgreSQL test database and rejects unsafe configurations that could point tests at the development database.

### Frontend

```bash
cd dashboard
npm ci
npm test
npm run build
```

### Verification

The dashboard milestone was verified with:

- 564 passing backend tests.
- 20 passing frontend tests.
- TypeScript checks and a successful production build.
- A headless Chrome walkthrough against the running Docker Compose application.
- Manual verification of authentication, failed-event investigation, replay, and tenant-separated dashboards.

These results describe the verified dashboard milestone, not a continuously updated CI status.

---

## Engineering Decisions and Tradeoffs

IntegrationLab intentionally makes several architectural tradeoffs.

| Decision                                   | Reason                                                                   |
| ------------------------------------------ | ------------------------------------------------------------------------ |
| Webhook-driven synchronization             | React to CRM changes without continuously polling the source             |
| PostgreSQL-backed event inbox              | Preserve accepted events independently of worker availability            |
| Background workers                         | Decouple integration processing from HTTP request handling               |
| Database advisory locks                    | Coordinate synchronization of the same account across workers            |
| Idempotent processing                      | Handle duplicate deliveries and repeated synchronization safely          |
| Field ownership enforcement                | Protect data managed by the destination system                           |
| Automatic retries and dead-letter handling | Recover from temporary failures while making persistent failures visible |
| Tenant-scoped persistence                  | Enforce customer data separation throughout the backend                  |
| React dashboard                            | Give operators a usable interface for monitoring and recovery            |
| Docker Compose                             | Make the multi-service demonstration reproducible locally                |

Detailed technical decisions and alternatives are documented in [Engineering Decisions](docs/engineering-decisions.md).

---

## Project Scope and Limitations

IntegrationLab is a **portfolio proof of concept**, not a production SaaS offering.

The project uses fictional customers and locally hosted mock CRM, CMS, and identity services.

Important limitations include:

- No live Salesforce or production CMS connection.
- The mock identity provider has no production user management, MFA, or refresh tokens.
- Browser access tokens are stored in `sessionStorage` for this local demonstration.
- Dashboard sessions expire after 15 minutes.
- The dashboard displays the latest 100 events and refreshes through polling.
- Local HTTP services do not provide production TLS termination.
- Database row-level security is not enabled; tenant isolation is enforced by application logic.
- Rate limiting and token revocation are not implemented.
- Production deployment, security hardening, and external identity-provider integration are outside the current scope.

These limitations are documented rather than presented as completed production capabilities.

See [Security and Remaining Risks](docs/security.md).

---

## Project Documentation

| Document                                               | Description                                                |
| ------------------------------------------------------ | ---------------------------------------------------------- |
| [Codebase Guide](docs/codebase-guide.md)               | Repository structure and recommended reading order         |
| [Architecture](docs/architecture.md)                   | Components, persistence, data flow, and concurrency        |
| [Customer Requirements](docs/customer-requirements.md) | Fictional customer scenario and business requirements      |
| [Demo Script](docs/demo-script.md)                     | Guided walkthrough of integration failures and recovery    |
| [API Contracts](docs/api-contracts.md)                 | REST API behavior and request/response contracts           |
| [Integration Guide](docs/integration-guide.md)         | Integration configuration and operational guidance         |
| [Security](docs/security.md)                           | Authentication, authorization, tenant isolation, and risks |
| [Troubleshooting](docs/troubleshooting.md)             | Diagnosing common integration and infrastructure issues    |
| [Engineering Decisions](docs/engineering-decisions.md) | Architecture decisions and implementation tradeoffs        |

---

## What This Project Demonstrates

IntegrationLab was developed to explore the intersection of **software engineering, enterprise integrations, and customer-facing technical problem solving**.

It demonstrates the ability to:

- Translate a customer integration scenario into technical requirements.
- Design and implement REST API and webhook-based integration workflows.
- Work with authentication, authorization, and tenant isolation.
- Build reliable background processing and recovery mechanisms.
- Model and persist operational data in PostgreSQL.
- Develop a React dashboard backed by a Python API.
- Test and troubleshoot a multi-service application.
- Communicate architectural decisions and technical tradeoffs.

The project was developed with AI-assisted engineering workflows, with architectural decisions, integration behavior, testing, and live verification forming part of the development process.

**IntegrationLab demonstrates not only how enterprise systems exchange data, but how those integrations can be monitored, secured, diagnosed, and recovered when something goes wrong.**
