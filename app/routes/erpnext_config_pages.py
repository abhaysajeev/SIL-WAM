"""ERPNext Config — list + form page routes."""
import os
import uuid as _uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_page_user
from app.core.doctypes import ERPNEXT_CONFIG_DOCTYPE
from app.models.company import Company
from app.models.erpnext_config import ERPNextConfig

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

if "dv_search_text" not in templates.env.filters:
    templates.env.filters["dv_search_text"] = (
        lambda r: " ".join(str(v) for v in r.values() if v is not None).lower()
    )

router = APIRouter(tags=["Pages"])


@router.get("/erpnext-configs", response_class=HTMLResponse)
def erpnext_config_list(
    request: Request,
    ctx=Depends(get_page_user),
    db: Session = Depends(get_db),
):
    if not ctx["perms"].get("erpnext_configs", {}).get("read"):
        return HTMLResponse("<h2>Access denied</h2>", status_code=403)

    user_company_id = ctx["user"].get("company_id")
    companies = {str(c.id): c.name for c in db.query(Company).all()}

    q = db.query(ERPNextConfig)
    if user_company_id:
        q = q.filter(ERPNextConfig.company_id == user_company_id)
    configs = q.order_by(ERPNextConfig.created_at.desc()).all()

    rows = [
        {
            "id":           str(c.id),
            "base_url":     c.base_url,
            "company_name": companies.get(str(c.company_id), "—"),
            "pdf_method":   c.pdf_method or "—",
            "is_active":    c.is_active,
            "created_at":   c.created_at.strftime("%Y-%m-%d %H:%M") if c.created_at else "—",
        }
        for c in configs
    ]

    return templates.TemplateResponse("layouts/list_view.html", {
        "request": request,
        "user":    ctx["user"],
        "perms":   ctx["perms"],
        "dt":      ERPNEXT_CONFIG_DOCTYPE,
        "rows":    rows,
        "active":  "erpnext_configs",
    })


@router.get("/erpnext-configs/new", response_class=HTMLResponse)
def erpnext_config_new(
    request: Request,
    ctx=Depends(get_page_user),
    db: Session = Depends(get_db),
):
    if not ctx["perms"].get("erpnext_configs", {}).get("create"):
        return HTMLResponse("<h2>Access denied</h2>", status_code=403)

    companies = db.query(Company).order_by(Company.name).all()

    return templates.TemplateResponse("layouts/form_view.html", {
        "request":   request,
        "user":      ctx["user"],
        "perms":     ctx["perms"],
        "dt":        ERPNEXT_CONFIG_DOCTYPE,
        "record":    {},
        "record_id": None,
        "companies": companies,
        "roles":     [],
        "active":    "erpnext_configs",
    })


@router.get("/erpnext-configs/{config_id}", response_class=HTMLResponse)
def erpnext_config_edit(
    config_id: str,
    request: Request,
    ctx=Depends(get_page_user),
    db: Session = Depends(get_db),
):
    if not ctx["perms"].get("erpnext_configs", {}).get("read"):
        return HTMLResponse("<h2>Access denied</h2>", status_code=403)

    try:
        oid = _uuid.UUID(config_id)
    except ValueError:
        return HTMLResponse("<h2>Not found</h2>", status_code=404)

    cfg = db.query(ERPNextConfig).filter(ERPNextConfig.id == oid).first()
    if not cfg:
        return HTMLResponse("<h2>ERPNext config not found</h2>", status_code=404)

    user_company_id = ctx["user"].get("company_id")
    if user_company_id and str(cfg.company_id) != user_company_id:
        return HTMLResponse("<h2>Access denied</h2>", status_code=403)

    companies = db.query(Company).order_by(Company.name).all()

    company = db.query(Company).filter(Company.id == cfg.company_id).first()
    record = {
        "id":         str(cfg.id),
        "company_id": company.name if company else str(cfg.company_id),
        "base_url":   cfg.base_url,
        "api_key":    cfg.api_key,
        "api_secret": "__saved__" if cfg.api_secret else "",  # sentinel: show masked if saved
        "pdf_method": cfg.pdf_method or "",
        "is_active":  cfg.is_active,
    }

    return templates.TemplateResponse("layouts/form_view.html", {
        "request":   request,
        "user":      ctx["user"],
        "perms":     ctx["perms"],
        "dt":        ERPNEXT_CONFIG_DOCTYPE,
        "record":    record,
        "record_id": str(cfg.id),
        "companies": companies,
        "roles":     [],
        "active":    "erpnext_configs",
    })
