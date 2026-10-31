# SIL-WAM — Claude Code Project Memory

## What This Project Is

SIL WhatsApp Manager — internal admin panel for an ISV hosting WhatsApp Business
messaging for multiple client companies. FastAPI + Jinja2 SSR backend, PostgreSQL DB.

## Full Project Map

See `PROJECT_MAP.md` in the repo root for the complete context map: data model /
FK graph, permission resources, every router + its guard, auth flows, background
schedulers, and the client-facing ingestion API. Read it before adding new logic
that touches models, routes, or permissions — it's the fastest way to see how a
new piece connects to the existing system. Keep it updated when adding a resource
(see "Adding a New Resource" below).

## How to Run

```bash
# Dev server
venv/bin/uvicorn app.main:app --reload

# Migrate DB
venv/bin/alembic upgrade head

# Tests (119+ tests, must all pass before committing)
venv/bin/pytest tests/ -v

# Create super admin (only way — no UI)
venv/bin/python run.py create-superadmin <username> <password>
```

## Critical Conventions

- **Never use `Base.metadata.create_all`** — tables are managed by Alembic only
- **Sync SQLAlchemy** — do not use `async def` in route handlers that call `get_db()`
- **super_admin = code bypass** — no permission rows in DB; code checks `role_name == "super_admin"` directly
- **super_admin cannot be created/edited/deleted from UI** — CLI only
- **NULL role_id SQL filter** — `(col != val) | col.is_(None)` because `NULL != UUID` = NULL in SQL
- **Page routes build templates with `build_templates()`** from `app/core/templating.py` —
  never `Jinja2Templates(...)` directly. It registers every shared filter and global
  (`dv_search_text`, `ts`, `page_url`) in one place, so a new route file gets them for free.
- **A customer's message history lives in `agent_threads` / `agent_messages`**, written
  only through `app/services/agent_timeline.py`. Never write those tables directly: the
  `seq` must be allocated with `UPDATE ... RETURNING` under the thread's row lock, or the
  sequence gaps and the app's "everything after seq N" sync silently loses messages.
  Timeline writes join the caller's transaction, never commit, and are wrapped in a
  SAVEPOINT so a failure there can never discard the caller's staged work.
- **Only an inbound message opens the 24-hour window.** Sending never does. Set
  `last_inbound_at` from `record_inbound` and nowhere else — treating a template send as
  opening the window produces free-text sends Meta rejects with 131047.
- **Broadcast tuning is per-company** — read it with `broadcast_config.get(db, company_id)`
  from `app/core/broadcast_config.py`, never a hardcoded constant. It always returns a
  usable frozen snapshot, falling back to `DEFAULTS` for a company with no row, and it
  clamps every value it reads so a bad row degrades rather than stalling the queue.
- **Worker threads must never touch a mapped object.** `expire_on_commit` is on, so an
  attribute read after a commit is a lazy SELECT on a Session owned by another thread.
  Copy what you need into a frozen dataclass on the main thread first — see `_Creds`,
  `_Tpl` and `_Job` in `broadcast_scheduler.py`. This is also why send concurrency costs
  threads and not connections from the shared pool.
- **Never `.strftime()` a timestamp for display.** Storage and the JSON APIs are UTC; only
  the browser knows the viewer's timezone. Print `{{ ts(value) }}` — or `{{ ts(value, 'date') }}` /
  `'time'` — which emits a `<time>` tag that `base.html` localises. In a list_view doctype,
  give the column `"type": "datetime"` (or `"date"`/`"time"`) and pass the raw value from the
  route, not a pre-formatted string.

## Permission Guards — Never Omit These

Every API route that touches data MUST have a `require()` guard. Forgetting it silently
exposes the endpoint with no access control.

### API routes (`/api/*`)

```python
# Router-level guard — applies to ALL routes in the router
router = APIRouter(
    prefix="/api/companies",
    dependencies=[Depends(require("companies", "read"))],
)

# Route-level guard — for stricter actions on top of router guard
@router.post("/")
def create(payload: ..., _user=Depends(require("companies", "create")), db=Depends(get_db)):
    ...

# super_admin only
@router.get("/")
def list_all(_user=Depends(require_super_admin), db=Depends(get_db)):
    ...
```

### Page routes (`/dashboard`, `/companies`, etc.)

Page routes use `get_page_user()` and check `ctx["perms"]` manually:

```python
@router.get("/companies")
def companies_list(request: Request, ctx=Depends(get_page_user), db=Depends(get_db)):
    if not ctx["perms"].get("companies", {}).get("read"):
        return HTMLResponse("<h2>Access denied</h2>", status_code=403)
    ...
```

### Company scoping — ALWAYS apply on data queries

Any user with `company_id` set must only see their own company's data.
Apply on EVERY list and get route for company-scoped resources:

```python
from app.core.deps import company_filter

# List
cid = company_filter(user)
q = db.query(Model)
if cid:
    q = q.filter(Model.company_id == cid)

# Get / Edit / Delete
cid = company_filter(user)
if cid and str(record.company_id) != cid:
    raise HTTPException(403, "Access denied")
```

`company_filter(user)` returns `None` for admin-tier users (sees all), or a UUID string for
company-scoped users. Never skip this — the bug where Abhay saw all companies was caused by
omitting it.

### Adding a new resource to the permission system

1. Add entry to `app/core/resources.py` RESOURCES list — this makes it appear in the
   permission matrix UI automatically
2. Use `require("resource_name", "action")` in the API router/routes
3. Check `ctx["perms"]["resource_name"]["read"]` in the page route
4. Add sidebar link in `base.html` gated by `{% if perms.resource_name.read %}`
5. **Add a migration granting the resource to the `admin` role.** The original seed
   granted admin only the pages that existed when it ran, so anything added later is
   invisible to every non-super-admin until a migration says so. See
   `c1f8a3d76e42` (agents) and `f2b5d8c03e49` (broadcast_settings).
   **The test suite cannot catch this** — tests authenticate as `super_admin`, which
   bypasses `require()` entirely, so the page passes every test and then 403s for the
   only people meant to use it. Verify by logging in as an `admin`.
6. Do NOT add Messaging-style resources to RESOURCES until the routes actually exist

## Auth Pattern

- `/api/*` routes: JWT Bearer (`get_current_user`, `require(page, action)`, `require_super_admin`)
- Page routes (`/dashboard`, `/companies`, etc.): session cookie (`get_page_user`)
- Not authenticated → `_LoginRedirect` exception → `RedirectResponse("/login")` via handler in `main.py`

## Adding a New Resource (full flow)

1. `app/models/<name>.py` — SQLAlchemy model
2. `app/models/__init__.py` — import it
3. `alembic/env.py` — import it
4. `venv/bin/alembic revision --autogenerate -m "add <name>"` + review + `upgrade head`
5. `app/schemas/<name>.py` — Pydantic Out/Create/Update schemas
6. `app/api/<name>_api.py` — CRUD with `require()` guards
7. `app/routes/<name>_pages.py` — SSR list + form routes; use `build_templates()`
8. `app/core/doctypes.py` — add doctype config dict
9. `app/templates/base.html` — add sidebar link with permission check
10. `app/main.py` — `include_router` for both API and page routers
11. `app/core/resources.py` — add page to RESOURCES if permission-controlled

## Key Files

| File | What it does |
|------|-------------|
| `app/core/deps.py` | All auth dependencies — `get_current_user`, `require`, `get_page_user`, `_LoginRedirect` |
| `app/core/resources.py` | RESOURCES list — valid pages and actions |
| `app/core/doctypes.py` | List/form UI config for each resource |
| `app/utils/error_logger.py` | `log_error()` — call this on unexpected exceptions in API handlers |
| `app/templates/layouts/list_view.html` | Universal list (receives `dt`, `rows`, `perms`, `user`) |
| `app/templates/layouts/form_view.html` | Universal form (receives `dt`, `record`, `record_id`, `roles`, `companies`) |
| `tests/conftest.py` | `make_user()`, `make_role()`, `login()`, fixtures |

## Seeded Role UUIDs (hardcoded in migrations + code)

```
super_admin    00000000-0000-0000-0000-000000000001  (is_system=True)
admin          00000000-0000-0000-0000-000000000002
company_viewer 00000000-0000-0000-0000-000000000003
```

## Error Logging

Call `log_error()` when an unexpected exception occurs in an API handler:

```python
from app.utils.error_logger import log_error

try:
    db.commit()
except Exception as e:
    db.rollback()
    log_error("Descriptive title", f"POST /api/resource/", e,
              request=request, request_data=payload.model_dump(), user=str(current_user.id))
    raise HTTPException(500, "Internal error")
```

The global `@app.exception_handler(Exception)` in `main.py` catches anything you miss.
Error log admin UI: `/error-logs` (super_admin only).

## Test IDs

`00000000-0000-0000-0000-000000000999` — intentional stub UUID for "record does not exist" tests. Not a bug.
