import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.core.deps import get_page_user
from app.core.templating import build_templates

router = APIRouter(tags=["Pages"])

BASE_DIR  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
templates = build_templates()


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
