"""SSR dashboard page."""
import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_page_user

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

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
