"""Broadcast Settings — list + form page routes."""
import uuid as _uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_page_user
from app.core.doctypes import BROADCAST_SETTINGS_DOCTYPE
from app.core.templating import build_templates
from app.models.broadcast_settings import BroadcastSettings
from app.models.company import Company

templates = build_templates()

router = APIRouter(tags=["Pages"])


@router.get("/broadcast-settings", response_class=HTMLResponse)
def broadcast_settings_list(
    request: Request,
    ctx=Depends(get_page_user),
    db: Session = Depends(get_db),
):
    if not ctx["perms"].get("broadcast_settings", {}).get("read"):
        return HTMLResponse("<h2>Access denied</h2>", status_code=403)

    user_company_id = ctx["user"].get("company_id")
    companies = {str(c.id): c.name for c in db.query(Company).all()}

    q = db.query(BroadcastSettings)
    if user_company_id:
        q = q.filter(BroadcastSettings.company_id == user_company_id)
    rows_db = q.order_by(BroadcastSettings.created_at.desc()).all()

    rows = [
        {
            "id":              str(s.id),
            "company_name":    companies.get(str(s.company_id), "—"),
            # Rendered here rather than in the template so the list reads as prose
            # instead of a grid of bare integers whose units the reader has to guess.
            "agent_limit":     ("unlimited" if s.agent_max_recipients_per_send == 0
                                else f"{s.agent_max_recipients_per_send} per send"),
            "send_concurrency": f"{s.send_concurrency} at a time",
            "max_retries":     ("no retries" if s.max_retries == 0
                                else f"{s.max_retries} retries"),
            "receipt_timeout": f"{s.receipt_timeout_minutes} min",
            # Raw value: list_view renders it as a <time> tag the browser localises.
            # Never pre-format a timestamp here — see CLAUDE.md.
            "updated_at":      s.updated_at,
        }
        for s in rows_db
    ]

    return templates.TemplateResponse("layouts/list_view.html", {
        "request": request,
        "user":    ctx["user"],
        "perms":   ctx["perms"],
        "dt":      BROADCAST_SETTINGS_DOCTYPE,
        "rows":    rows,
        "active":  "broadcast_settings",
    })


@router.get("/broadcast-settings/new", response_class=HTMLResponse)
def broadcast_settings_new(
    request: Request,
    ctx=Depends(get_page_user),
    db: Session = Depends(get_db),
):
    if not ctx["perms"].get("broadcast_settings", {}).get("create"):
        return HTMLResponse("<h2>Access denied</h2>", status_code=403)

    companies = db.query(Company).order_by(Company.name).all()

    # Prefilled with the system defaults rather than left blank, so the form shows
    # what the company is getting today and an edit is visibly a change from it.
    from app.core.broadcast_config import DEFAULTS
    record = {
        "agent_max_recipients_per_send": DEFAULTS.agent_max_recipients_per_send,
        "agent_daily_message_limit":     DEFAULTS.agent_daily_message_limit,
        "send_concurrency":              DEFAULTS.send_concurrency,
        "batch_size":                    DEFAULTS.batch_size,
        "max_retries":                   DEFAULTS.max_retries,
        "retry_backoff_seconds":         DEFAULTS.retry_backoff_seconds,
        "receipt_timeout_minutes":       DEFAULTS.receipt_timeout_minutes,
        "stale_claim_minutes":           DEFAULTS.stale_claim_minutes,
    }

    return templates.TemplateResponse("layouts/form_view.html", {
        "request":   request,
        "user":      ctx["user"],
        "perms":     ctx["perms"],
        "dt":        BROADCAST_SETTINGS_DOCTYPE,
        "record":    record,
        "record_id": None,
        "companies": companies,
        "roles":     [],
        "active":    "broadcast_settings",
    })


@router.get("/broadcast-settings/{settings_id}", response_class=HTMLResponse)
def broadcast_settings_edit(
    settings_id: str,
    request: Request,
    ctx=Depends(get_page_user),
    db: Session = Depends(get_db),
):
    if not ctx["perms"].get("broadcast_settings", {}).get("read"):
        return HTMLResponse("<h2>Access denied</h2>", status_code=403)

    try:
        oid = _uuid.UUID(settings_id)
    except ValueError:
        return HTMLResponse("<h2>Not found</h2>", status_code=404)

    row = db.query(BroadcastSettings).filter(BroadcastSettings.id == oid).first()
    if not row:
        return HTMLResponse("<h2>Broadcast settings not found</h2>", status_code=404)

    user_company_id = ctx["user"].get("company_id")
    if user_company_id and str(row.company_id) != user_company_id:
        return HTMLResponse("<h2>Access denied</h2>", status_code=403)

    companies = db.query(Company).order_by(Company.name).all()
    company = db.query(Company).filter(Company.id == row.company_id).first()

    record = {
        "id":         str(row.id),
        # The company field is create_only, so the form shows the name as read-only
        # text — the same treatment ERPNext Config gives it.
        "company_id": company.name if company else str(row.company_id),
        "agent_max_recipients_per_send": row.agent_max_recipients_per_send,
        "agent_daily_message_limit":     row.agent_daily_message_limit,
        "send_concurrency":              row.send_concurrency,
        "batch_size":                    row.batch_size,
        "max_retries":                   row.max_retries,
        "retry_backoff_seconds":         row.retry_backoff_seconds,
        "receipt_timeout_minutes":       row.receipt_timeout_minutes,
        "stale_claim_minutes":           row.stale_claim_minutes,
    }

    return templates.TemplateResponse("layouts/form_view.html", {
        "request":   request,
        "user":      ctx["user"],
        "perms":     ctx["perms"],
        "dt":        BROADCAST_SETTINGS_DOCTYPE,
        "record":    record,
        "record_id": str(row.id),
        "companies": companies,
        "roles":     [],
        "active":    "broadcast_settings",
    })
