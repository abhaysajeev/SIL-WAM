"""Users — list view + form view (universal templates)."""
import os
import uuid as uuid_mod

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_page_user
from app.core.doctypes import USERS_DOCTYPE
from app.models.company import Company
from app.models.role import Role
from app.models.user import User

_SUPER_ADMIN_ROLE_ID = uuid_mod.UUID("00000000-0000-0000-0000-000000000001")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


def _dv_search_text(row: dict) -> str:
    return " ".join(str(v) for v in row.values() if v is not None).lower()

templates.env.filters["dv_search_text"] = _dv_search_text


router = APIRouter(tags=["Pages"])


def _user_to_dict(u: User, role_map: dict, company_map: dict) -> dict:
    return {
        "id":                   str(u.id),
        "username":             u.username,
        "full_name":            u.full_name,
        "phone":                u.phone,
        "role_id":              str(u.role_id) if u.role_id else None,
        "company_id":           str(u.company_id) if u.company_id else None,
        "is_active":            u.is_active,
        "must_change_password": u.must_change_password,
        # resolved display values for list view
        "_role_name":    role_map.get(str(u.role_id)) if u.role_id else None,
        "_company_name": company_map.get(str(u.company_id)) if u.company_id else None,
    }


# ── List view ──────────────────────────────────────────────

@router.get("/users", response_class=HTMLResponse)
def users_list(
    request: Request,
    ctx=Depends(get_page_user),
    db: Session = Depends(get_db),
):
    if not ctx["perms"].get("users", {}).get("read"):
        return HTMLResponse("<h2>Access denied</h2>", status_code=403)

    query = db.query(User).filter(
        (User.role_id != _SUPER_ADMIN_ROLE_ID) | (User.role_id.is_(None))
    )
    if ctx["user"]["company_id"]:
        query = query.filter(User.company_id == ctx["user"]["company_id"])

    role_map    = {str(r.id): r.display_name for r in db.query(Role).all()}
    company_map = {str(c.id): c.name for c in db.query(Company).all()}

    rows = [_user_to_dict(u, role_map, company_map)
            for u in query.order_by(User.username).all()]

    return templates.TemplateResponse("layouts/list_view.html", {
        "request": request,
        "user":    ctx["user"],
        "perms":   ctx["perms"],
        "dt":      USERS_DOCTYPE,
        "rows":    rows,
        "active":  "users",
    })


# ── Form view — new ────────────────────────────────────────

@router.get("/users/new", response_class=HTMLResponse)
def user_new(
    request: Request,
    ctx=Depends(get_page_user),
    db: Session = Depends(get_db),
):
    if not ctx["perms"].get("users", {}).get("create"):
        return HTMLResponse("<h2>Access denied</h2>", status_code=403)

    roles     = db.query(Role).filter(Role.id != _SUPER_ADMIN_ROLE_ID).order_by(Role.display_name).all()
    companies = db.query(Company).filter(Company.is_active == True).order_by(Company.name).all()

    return templates.TemplateResponse("layouts/form_view.html", {
        "request":   request,
        "user":      ctx["user"],
        "perms":     ctx["perms"],
        "dt":        USERS_DOCTYPE,
        "record":    None,
        "record_id": None,
        "roles":     roles,
        "companies": companies,
        "active":    "users",
    })


# ── Form view — edit ───────────────────────────────────────

@router.get("/users/{user_id}", response_class=HTMLResponse)
def user_edit(
    user_id: uuid_mod.UUID,
    request: Request,
    ctx=Depends(get_page_user),
    db: Session = Depends(get_db),
):
    if not ctx["perms"].get("users", {}).get("read"):
        return HTMLResponse("<h2>Access denied</h2>", status_code=403)

    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        return HTMLResponse("<h2>User not found</h2>", status_code=404)

    if u.role_id == _SUPER_ADMIN_ROLE_ID:
        return HTMLResponse("<h2>Access denied</h2>", status_code=403)

    roles     = db.query(Role).filter(Role.id != _SUPER_ADMIN_ROLE_ID).order_by(Role.display_name).all()
    companies = db.query(Company).filter(Company.is_active == True).order_by(Company.name).all()

    role_map    = {str(r.id): r.display_name for r in roles}
    company_map = {str(c.id): c.name for c in companies}

    return templates.TemplateResponse("layouts/form_view.html", {
        "request":   request,
        "user":      ctx["user"],
        "perms":     ctx["perms"],
        "dt":        USERS_DOCTYPE,
        "record":    _user_to_dict(u, role_map, company_map),
        "record_id": str(u.id),
        "roles":     roles,
        "companies": companies,
        "active":    "users",
    })
