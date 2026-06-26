import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.core.deps import get_page_user

router = APIRouter(tags=["Pages"])

BASE_DIR  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


@router.get("/demo-messaging", response_class=HTMLResponse)
def demo_messaging_page(request: Request, ctx=Depends(get_page_user)):
    if ctx["user"]["role_name"] != "super_admin":
        return HTMLResponse("<h2>Access denied</h2>", status_code=403)
    return templates.TemplateResponse("demo/messaging.html", {
        "request": request,
        "user":    ctx["user"],
        "perms":   ctx["perms"],
        "active":  "demo_messaging",
    })
