"""SSR auth page routes — login page and session logout."""
import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

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
