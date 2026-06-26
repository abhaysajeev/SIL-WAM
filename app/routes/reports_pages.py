"""SSR route for the /reports analytics page."""
import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.core.database import get_db  # noqa: F401 — imported for consistency
from app.core.deps import get_page_user

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

router = APIRouter(tags=["Pages"])


@router.get("/reports", response_class=HTMLResponse)
def reports_page(
    request: Request,
    ctx=Depends(get_page_user),
):
    if not ctx["perms"].get("reports", {}).get("read"):
        return HTMLResponse("<h2>Access denied</h2>", status_code=403)

    return templates.TemplateResponse("analytics/index.html", {
        "request": request,
        "user":    ctx["user"],
        "perms":   ctx["perms"],
        "active":  "reports",
    })
