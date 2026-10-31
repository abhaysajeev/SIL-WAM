"""SSR dashboard page."""
import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.templating import build_templates
from app.core.deps import get_page_user

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
templates = build_templates()

router = APIRouter(tags=["Pages"])


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    ctx=Depends(get_page_user),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse("dashboard/index.html", {
        "request": request,
        "user": ctx["user"],
        "perms": ctx["perms"],
        "active": "dashboard",
    })
