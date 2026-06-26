"""SSR roles and permission matrix pages."""
import os
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_page_user
from app.core.resources import RESOURCES, VALID_ACTIONS
from app.models.role import Role, RolePagePermission

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

router = APIRouter(tags=["Pages"])


@router.get("/roles", response_class=HTMLResponse)
def roles_list(
    request: Request,
    ctx=Depends(get_page_user),
    db: Session = Depends(get_db),
):
    # Only super_admin can manage roles
    if ctx["user"]["role_name"] != "super_admin":
        return HTMLResponse("<h2>Access denied</h2>", status_code=403)

    roles = db.query(Role).order_by(Role.display_name).all()
    return templates.TemplateResponse("roles/list.html", {
        "request": request,
        "user": ctx["user"],
        "perms": ctx["perms"],
        "roles": roles,
        "active": "roles",
    })


@router.get("/roles/{role_id}/permissions", response_class=HTMLResponse)
def role_permissions(
    role_id: uuid.UUID,
    request: Request,
    ctx=Depends(get_page_user),
    db: Session = Depends(get_db),
):
    if ctx["user"]["role_name"] != "super_admin":
        return HTMLResponse("<h2>Access denied</h2>", status_code=403)

    role = db.query(Role).filter(Role.id == role_id).first()
    if not role:
        return HTMLResponse("<h2>Role not found</h2>", status_code=404)

    # Build a dict: page_name → permission row (defaults all False)
    perm_rows = {
        r.page_name: r
        for r in db.query(RolePagePermission).filter(
            RolePagePermission.role_id == role_id
        ).all()
    }

    return templates.TemplateResponse("roles/permissions.html", {
        "request":     request,
        "user":        ctx["user"],
        "perms":       ctx["perms"],
        "role":        role,
        "resources":   RESOURCES,
        "perm_rows":   perm_rows,
        "perm_readonly": role.name == "super_admin",
        "active":      "roles",
    })
