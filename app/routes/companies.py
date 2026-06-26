"""Companies — list view + tabbed detail view."""
import os
import uuid as uuid_mod

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import get_page_user
from app.core.doctypes import COMPANIES_DOCTYPE
from app.models.company import Company
from app.models.whatsapp import WhatsAppAccount, WhatsAppTemplate

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Jinja2 filter: build a flat search string from a row dict
def _dv_search_text(row: dict) -> str:
    return " ".join(str(v) for v in row.values() if v is not None).lower()

templates.env.filters["dv_search_text"] = _dv_search_text

router = APIRouter(tags=["Pages"])


def _company_to_dict(c: Company) -> dict:
    return {
        "id":           str(c.id),
        "name":         c.name,
        "company_code": c.company_code,
        "is_active":    c.is_active,
    }


# ── List view ──────────────────────────────────────────────

@router.get("/companies", response_class=HTMLResponse)
def companies_list(
    request: Request,
    ctx=Depends(get_page_user),
    db: Session = Depends(get_db),
):
    if not ctx["perms"].get("companies", {}).get("read"):
        return HTMLResponse("<h2>Access denied</h2>", status_code=403)

    company_id = ctx["user"].get("company_id")
    q = db.query(Company)
    if company_id:
        q = q.filter(Company.id == company_id)
    rows = [_company_to_dict(c) for c in q.order_by(Company.name).all()]

    return templates.TemplateResponse("layouts/list_view.html", {
        "request": request,
        "user":    ctx["user"],
        "perms":   ctx["perms"],
        "dt":      COMPANIES_DOCTYPE,
        "rows":    rows,
        "active":  "companies",
    })


# ── Form view — new ────────────────────────────────────────

@router.get("/companies/new", response_class=HTMLResponse)
def company_new(
    request: Request,
    ctx=Depends(get_page_user),
    db: Session = Depends(get_db),
):
    if not ctx["perms"].get("companies", {}).get("create"):
        return HTMLResponse("<h2>Access denied</h2>", status_code=403)

    return templates.TemplateResponse("layouts/form_view.html", {
        "request":   request,
        "user":      ctx["user"],
        "perms":     ctx["perms"],
        "dt":        COMPANIES_DOCTYPE,
        "record":    None,
        "record_id": None,
        "roles":     [],
        "companies": [],
        "active":    "companies",
    })


# ── Form view — edit ───────────────────────────────────────

@router.get("/companies/{company_id}", response_class=HTMLResponse)
def company_edit(
    company_id: uuid_mod.UUID,
    request: Request,
    ctx=Depends(get_page_user),
    db: Session = Depends(get_db),
):
    if not ctx["perms"].get("companies", {}).get("read"):
        return HTMLResponse("<h2>Access denied</h2>", status_code=403)

    user_company_id = ctx["user"].get("company_id")
    if user_company_id and str(company_id) != user_company_id:
        return HTMLResponse("<h2>Access denied</h2>", status_code=403)

    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        return HTMLResponse("<h2>Company not found</h2>", status_code=404)

    wa = db.query(WhatsAppAccount).filter(
        WhatsAppAccount.company_id == company_id
    ).first()
    wa_data = None
    if wa:
        wa_data = {
            "id": str(wa.id),
            "waba_id": wa.waba_id,
            "phone_number_id": wa.phone_number_id,
            "display_phone_number": wa.display_phone_number,
            "business_name": wa.business_name,
            "business_id": wa.business_id,
            "connection_status": wa.connection_status,
            "last_sync_at": wa.last_sync_at.isoformat() if wa.last_sync_at else None,
        }

    tpl_rows = db.query(WhatsAppTemplate).filter(
        WhatsAppTemplate.company_id == company_id
    ).order_by(WhatsAppTemplate.created_at.desc()).all()
    templates_data = [
        {
            "id": str(t.id),
            "name": t.name,
            "category": t.category,
            "language": t.language,
            "status": t.status,
            "rejection_reason": t.rejection_reason,
            "components": t.components or [],
            "param_mapping": t.param_mapping or {},
            "cta_mapping": t.cta_mapping or {},
            "mobile_mapping": t.mobile_mapping or "",
            "synced_at": t.synced_at.isoformat() if t.synced_at else None,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in tpl_rows
    ]

    return templates.TemplateResponse("companies/detail.html", {
        "request":          request,
        "user":             ctx["user"],
        "perms":            ctx["perms"],
        "dt":               COMPANIES_DOCTYPE,
        "record":           _company_to_dict(company),
        "record_id":        str(company.id),
        "roles":            [],
        "companies":        [],
        "active":           "companies",
        "whatsapp_account": wa_data,
        "fb_app_id":        settings.FB_APP_ID,
        "meta_config_id":   settings.META_CONFIG_ID,
        "templates_data":   templates_data,
    })
