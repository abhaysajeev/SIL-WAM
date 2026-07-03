import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.core.config import settings
from app.core.database import Base
from app.core.deps import _LoginRedirect
from app.models import api_key, company, conversation, erpnext_config, error_log, role, user, whatsapp  # noqa: F401
from app.utils.error_logger import log_error

# ── API routers ───────────────────────────────────────────────────────────────
from app.api.auth import router as auth_router
from app.api.companies import router as companies_api_router
from app.api.users_api import router as users_api_router
from app.api.roles_api import router as roles_api_router
from app.api.error_logs_api import router as error_logs_api_router
from app.api.whatsapp_api import router as whatsapp_api_router
from app.api.company_api_keys_api import router as company_api_keys_api_router
from app.api.erpnext_config_api import router as erpnext_config_api_router
from app.api.meta_webhook import router as meta_webhook_router
from app.api.analytics_api import router as analytics_router
from app.api.client_services_api import router as client_services_router
from app.api.sse_api import router as sse_router
from app.api.erpnext_webhook import router as erpnext_webhook_router
from app.api.webhook_config_api import router as webhook_config_api_router
from app.api.demo_api import router as demo_api_router

# ── SSR page routers ──────────────────────────────────────────────────────────
from app.routes.auth_pages import router as auth_pages_router
from app.routes.dashboard import router as dashboard_router
from app.routes.companies import router as companies_router
from app.routes.users_pages import router as users_pages_router
from app.routes.roles_pages import router as roles_pages_router
from app.routes.error_logs_pages import router as error_logs_pages_router
from app.routes.erpnext_config_pages import router as erpnext_config_pages_router
from app.routes.reports_pages import router as reports_pages_router
from app.routes.services_pages import router as services_pages_router
from app.routes.conversations_pages import router as conversations_pages_router
from app.routes.webhook_config_pages import router as webhook_config_pages_router
from app.routes.demo_pages import router as demo_pages_router

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)

templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Tables are managed by Alembic migrations — never auto-create here
    from app.services import expiry_scheduler, notify_scheduler, send_scheduler
    expiry_scheduler.start()
    send_scheduler.start()
    notify_scheduler.start()
    yield
    expiry_scheduler.stop()
    send_scheduler.stop()
    notify_scheduler.stop()


app = FastAPI(title=settings.APP_TITLE, lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.SECRET_KEY,
    session_cookie="wam_session",
    max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60,
    https_only=settings.HTTPS_ONLY,
    same_site="lax",
)

app.mount("/static", StaticFiles(directory=os.path.join(ROOT_DIR, "static")), name="static")


# ── Exception handlers ────────────────────────────────────────────────────────
@app.exception_handler(_LoginRedirect)
async def login_redirect_handler(request: Request, exc: _LoginRedirect):
    return RedirectResponse(url="/login", status_code=302)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    if isinstance(exc, HTTPException):
        return await http_exception_handler(request, exc)
    if isinstance(exc, RequestValidationError):
        return await request_validation_exception_handler(request, exc)
    if isinstance(exc, _LoginRedirect):
        return RedirectResponse(url="/login", status_code=302)

    req_data = None
    try:
        body = await request.body()
        if body:
            req_data = json.loads(body)
    except Exception:
        pass

    error_id = await run_in_threadpool(
        log_error,
        f"Unhandled {type(exc).__name__}",
        f"{request.method} {request.url.path}",
        exc,
        request,
        req_data,
    )
    return JSONResponse(
        status_code=500,
        content={"success": False, "message": "An internal error occurred.", "error_id": error_id},
    )


# ── API routes ────────────────────────────────────────────────────────────────
app.include_router(auth_router, prefix="/api")
app.include_router(companies_api_router)
app.include_router(users_api_router)
app.include_router(roles_api_router)
app.include_router(error_logs_api_router)
app.include_router(whatsapp_api_router)
app.include_router(company_api_keys_api_router)
app.include_router(erpnext_config_api_router)
app.include_router(meta_webhook_router)
app.include_router(analytics_router)
app.include_router(sse_router)
app.include_router(client_services_router)
app.include_router(erpnext_webhook_router)
app.include_router(webhook_config_api_router)
app.include_router(demo_api_router)

# ── SSR page routes ───────────────────────────────────────────────────────────
app.include_router(auth_pages_router)
app.include_router(dashboard_router)
app.include_router(companies_router)
app.include_router(users_pages_router)
app.include_router(roles_pages_router)
app.include_router(error_logs_pages_router)
app.include_router(erpnext_config_pages_router)
app.include_router(reports_pages_router)
app.include_router(services_pages_router)
app.include_router(conversations_pages_router)
app.include_router(webhook_config_pages_router)
app.include_router(demo_pages_router)


@app.get("/", response_class=HTMLResponse)
async def root():
    return RedirectResponse(url="/login", status_code=302)
