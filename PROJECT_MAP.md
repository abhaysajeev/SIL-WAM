# SIL-WAM — Project Context Map

Generated from static analysis of the codebase (models, routers, deps, services) as of
2026-07-10. This is a reference document, not a source of truth — if it disagrees with
the code, the code wins. Regenerate/update sections when adding new resources, models,
or routers (see "Adding a New Resource" in CLAUDE.md).

## 1. Data model (SQLAlchemy, `app/models/`)

```
companies (Company)
 ├─< users (User.company_id, SET NULL)            company_id NULL = admin tier (sees all)
 │    └─< refresh_tokens (RefreshToken.user_id, CASCADE)
 │    users.role_id ──> roles (Role, SET NULL)
 │         └─< role_page_permission (RolePagePermission, CASCADE)  [composite PK: role_id+page_name]
 │    users.created_by_id ──> users.id (self-FK, SET NULL)
 │
 ├─< company_api_keys (CompanyApiKey, CASCADE)      X-API-Key auth for /client-api/v1/*
 │
 ├─< erpnext_configs (ERPNextConfig, CASCADE, unique per company)
 │
 ├─< whatsapp_accounts (WhatsAppAccount, CASCADE, unique per company)
 ├─< whatsapp_onboarding_sessions (WhatsAppOnboardingSession, CASCADE)
 ├─< whatsapp_templates (WhatsAppTemplate, CASCADE)
 │
 └─< conversations (Conversation, CASCADE)          unique (mobile_no, company_id)
      ├─< services (Service, CASCADE)               unique (service_id, company_id)
      │    │   service.template_id ──> whatsapp_templates (SET NULL)
      │    │   service.api_key_id  ──> company_api_keys (SET NULL) — resolves notify_url
      │    ├─< mobile_queue (MobileQueue, CASCADE)      per-service in_progress marker
      │    ├─< service_responses (ServiceResponse, CASCADE)  one per answered question
      │    └─< outbound_notifications (OutboundNotification, CASCADE)  status callback queue
      │         outbound_notifications.message_id ──> messages (CASCADE, nullable)
      └─< messages (Message, CASCADE)                every inbound/outbound WA message
           message.service_id ──> services (SET NULL, nullable)

error_log        — standalone, no FKs (unexpected-exception log, see log_error())
failed_webhooks  — standalone, no FKs (source: "meta" | "erpnext")
```

Key invariants:
- `services.status`: `waiting | in_progress | completed | expired | failed`. Multiple
  services can be `in_progress` concurrently for the same `(company_id, mobile_no)` —
  concurrency is unlimited (see `app/models/conversation.py` docstring).
- `User.company_id IS NULL` → admin tier, sees all companies. Otherwise scoped —
  enforce with `company_filter()` on every query (see CLAUDE.md "Company scoping").
- `User.role_id IS NULL` → no access at all (not "default" access).
- `super_admin` role is a code bypass, never has `role_page_permission` rows.

## 2. Permission resources (`app/core/resources.py`)

Single source of truth for what pages/actions exist. Anything not listed here cannot be
assigned in the permission matrix UI or checked by `require()`.

| Module     | Resource          | Actions                     |
|------------|-------------------|------------------------------|
| Main       | `dashboard`       | read                         |
| Management | `companies`       | read, create, write, delete  |
| Management | `users`           | read, create, write, delete  |
| Messaging  | `services`        | read                         |
| Messaging  | `conversations`   | read                         |
| Messaging  | `erpnext_configs` | read, create, write, delete  |
| Analytics  | `reports`         | read                         |

Not yet in RESOURCES (per CLAUDE.md, do not add until routes exist): none currently
pending — `whatsapp_api` routes are gated under the `companies` resource, not a
dedicated `whatsapp` resource.

## 3. Routers (`app/main.py` registration order)

### API routers (JWT Bearer via `get_current_user` / `require()` / `require_super_admin`)

| Prefix                     | File                            | Guard                              |
|-----------------------------|----------------------------------|-------------------------------------|
| `/api/auth`                | `api/auth.py`                   | none (login/refresh/logout/me)      |
| `/api/companies`           | `api/companies.py`              | `require("companies", *)`           |
| `/api/users`               | `api/users_api.py`              | `require("users", *)`               |
| `/api/roles`               | `api/roles_api.py`              | `require_super_admin`               |
| `/api/error-logs`          | `api/error_logs_api.py`         | `require_super_admin`               |
| `/api/whatsapp`            | `api/whatsapp_api.py`           | `require("companies", *)`           |
| `/api/company-api-keys`    | `api/company_api_keys_api.py`   | `require("companies", "write")`     |
| `/api/erpnext-configs`     | `api/erpnext_config_api.py`     | `require("erpnext_configs", *)`     |
| `/webhook/meta`            | `api/meta_webhook.py`           | none (Meta signature-verified)      |
| `/api/analytics`           | `api/analytics_api.py`          | `require("reports", "read")`        |
| `/api/stream` (SSE)        | `api/sse_api.py`                | (check file — session-based)        |
| `/client-api/v1`           | `api/client_services_api.py`    | `get_api_company` (X-API-Key header)|
| `/webhook/erpnext`         | `api/erpnext_webhook.py`        | none (ERPNext-signed callback)      |
| `/api/webhook-config`      | `api/webhook_config_api.py`     | `require_super_admin`               |
| `/api/demo`                | `api/demo_api.py`               | `require_super_admin`               |

### Page (SSR) routers (session cookie via `get_page_user`)

| Path(s)                                   | File                             |
|--------------------------------------------|-----------------------------------|
| `/login`, `/auth/logout`                  | `routes/auth_pages.py`            |
| `/dashboard`                              | `routes/dashboard.py`             |
| `/companies`, `/companies/new`, `/companies/{id}` | `routes/companies.py`      |
| `/users`, `/users/new`, `/users/{id}`     | `routes/users_pages.py`           |
| `/roles`, `/roles/{id}/permissions`       | `routes/roles_pages.py`           |
| `/error-logs`, `/error-logs/{id}`         | `routes/error_logs_pages.py`      |
| `/erpnext-configs*`                       | `routes/erpnext_config_pages.py`  |
| `/reports`                                | `routes/reports_pages.py`         |
| `/services`, `/services/{id}`             | `routes/services_pages.py`        |
| `/conversations`, `/conversations/{id}`   | `routes/conversations_pages.py`   |
| `/webhook-config`                         | `routes/webhook_config_pages.py`  |
| `/demo-messaging`                         | `routes/demo_pages.py`            |

Page routes check `ctx["perms"][resource]["action"]` manually — no router-level
dependency, per CLAUDE.md pattern.

## 4. Auth flows (`app/core/deps.py`)

- **`/api/*`** → `HTTPBearer` JWT → `get_current_user` → `require(page, action)` guard
  factory (queries `role_page_permission`) or `require_super_admin`.
- **Page routes** → `SessionMiddleware` cookie (`wam_session`) → `get_page_user` builds
  a full `perms` dict (all pages, all actions) injected into every Jinja2 template.
  Missing/invalid session → raises `_LoginRedirect` → caught by the app-level exception
  handler in `main.py` → 302 to `/login`.
- **`/client-api/v1/*`** (external, e.g. .NET SFA) → `X-API-Key` header →
  `get_api_company` / `get_api_key_and_company` → resolves `Company` /
  `CompanyApiKey` row directly, bypassing the role/permission system entirely.
- **Webhooks** (`/webhook/meta`, `/webhook/erpnext`) → verified by
  provider-specific signature/token checks inside the route, not the dep system.

## 5. Background services (`app/services/`, started in `main.py` lifespan)

Three `BackgroundScheduler` daemon threads, each opening its own DB session per job:

- **`send_scheduler`** — polls for `services` with `template_sent=False` at the front
  of the queue, dispatches the Meta template send via `wa_sender.py`
  (`SELECT ... FOR UPDATE SKIP LOCKED`, safe across multiple app instances).
- **`expiry_scheduler`** — Condition A: marks a service `expired`/`timeout` if the
  customer never taps the template button within `template_expiry_hours`, but only
  while zero questions have been answered yet.
- **`notify_scheduler`** — durable delivery of `outbound_notifications` rows to each
  client's `notify_url` (payload fully materialized at enqueue time by
  `notify_queue.py`; this poller just POSTs, no re-computation).

Request-time flow (not scheduled):
- **`conversation_engine.py`** — inbound message router / state machine, invoked from
  `meta_webhook.py` via `BackgroundTasks`. Dedups by `wamid`, resolves which `Service`
  a reply belongs to (via Meta's `context.id` for button taps, or "exactly one service
  has an outstanding free-text question" fallback for plain text), records
  `ServiceResponse`, advances or completes the flow.
- **`queue_manager.py`** — activates newly ingested services (no FastAPI/HTTPException
  imports; failures are logged via `log_error`, never raised, since it's called from
  background contexts).
- **`erpnext_client.py`** — fetches invoice PDFs from a company's ERPNext instance and
  uploads them to Meta media, triggered by QUICK_REPLY button taps.
- **`wa_sender.py`** — the only place that calls the Meta Graph API to send messages;
  never raises, returns a `SendResult`.
- **`meta_graph_client.py`** — Meta webhook subscription management (app access token).

## 6. Client-facing ingestion API (`/client-api/v1`, `client_services_api.py`)

External systems (e.g. the .NET SFA) authenticate with `X-API-Key` and:
- `POST /services` — ingest a new service/order → enqueued via `queue_manager`,
  template send happens asynchronously via `send_scheduler`.
- `GET /services/{service_id}` — poll status.
- `PATCH /services/{service_id}/retry` — retry a failed service.

Status changes flow back out via `outbound_notifications` → `notify_scheduler` → the
`notify_url` recorded on the `CompanyApiKey` that ingested the service (dashboard
status progression is enforced monotonic — see recent commits `e74691c`, `91eda32`).

## 7. Where to look for X

| You want to...                                   | Look at                                          |
|----------------------------------------------------|---------------------------------------------------|
| Add a new page/resource                           | CLAUDE.md § "Adding a New Resource (full flow)"   |
| Change a permission gate                          | `app/core/resources.py` + `deps.require()`        |
| Understand company data isolation                 | `app/core/deps.py::company_filter`                |
| Trace an inbound WhatsApp message                 | `app/api/meta_webhook.py` → `services/conversation_engine.py` |
| Trace an outbound template send                   | `client_services_api.py` → `queue_manager.py` → `send_scheduler.py` → `wa_sender.py` |
| Trace a client status callback                    | `notify_queue.py` (enqueue) → `notify_scheduler.py` (deliver) |
| Understand service expiry (no customer reply)     | `services/expiry_scheduler.py`                    |
| Find seeded role UUIDs                             | CLAUDE.md § "Seeded Role UUIDs"                   |
| Understand error logging                           | `app/utils/error_logger.py` + `/error-logs` UI    |
| See list/form UI config per doctype                | `app/core/doctypes.py`                            |
| Find existing test coverage for an area            | `tests/test_*.py` (17 files, 119+ tests, conftest has `make_user`/`make_role`/`login`) |

## 8. Not covered here

Schemas (`app/schemas/`) are thin Pydantic mirrors of the models above and aren't
mapped separately. `alembic/versions/` has 21 migrations — always generate new ones
with `alembic revision --autogenerate`, never hand-edit `Base.metadata`.
