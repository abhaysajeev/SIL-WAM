import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.core.config import settings
from app.core.deps import get_page_user
from app.services import meta_graph_client

router = APIRouter(tags=["Pages"])

BASE_DIR  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
templates.env.filters["dv_search_text"] = (
    lambda r: " ".join(str(v) for v in r.values() if v is not None).lower()
)


@router.get("/webhook-config", response_class=HTMLResponse)
def webhook_config_page(request: Request, ctx=Depends(get_page_user)):
    if ctx["user"]["role_name"] != "super_admin":
        return HTMLResponse("<h2>Access denied</h2>", status_code=403)

    return templates.TemplateResponse("webhook_config/index.html", {
        "request":               request,
        "user":                  ctx["user"],
        "perms":                 ctx["perms"],
        "active":                "webhook_config",
        "app_id_configured":     bool(settings.FB_APP_ID),
        "app_secret_configured": bool(settings.META_APP_SECRET),
        "verify_token_configured": bool(settings.META_WEBHOOK_VERIFY_TOKEN),
        "available_fields":      meta_graph_client.FIELDS_ALL,
        "required_fields":       list(meta_graph_client.FIELDS_REQUIRED),
    })
