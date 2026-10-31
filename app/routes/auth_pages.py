"""SSR auth page routes — login page and session logout."""
import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from app.core.templating import build_templates

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
templates = build_templates()

router = APIRouter(tags=["Pages"])


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    # If already logged in, go straight to dashboard
    if request.session.get("user_id"):
        return RedirectResponse(url="/dashboard", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request})


@router.get("/auth/logout")
async def logout_page(request: Request):
    """Clears the server-side session and redirects to login."""
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)
