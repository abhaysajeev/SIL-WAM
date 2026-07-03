"""Conversations — list + detail (message thread) page routes."""
import os
import uuid as _uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_page_user
from app.core.doctypes import CONVERSATIONS_DOCTYPE
from app.models.company import Company
from app.models.conversation import Conversation, Message, MobileQueue, Service, ServiceResponse

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

if "dv_search_text" not in templates.env.filters:
    templates.env.filters["dv_search_text"] = (
        lambda r: " ".join(str(v) for v in r.values() if v is not None).lower()
    )

router = APIRouter(tags=["Pages"])


# ── List ─────────────────────────────────────────────────────────────────────

@router.get("/conversations", response_class=HTMLResponse)
def conversations_list(
    request: Request,
    ctx=Depends(get_page_user),
    db: Session = Depends(get_db),
):
    if not ctx["perms"].get("conversations", {}).get("read"):
        return HTMLResponse("<h2>Access denied</h2>", status_code=403)

    user_company_id = ctx["user"].get("company_id")

    q = db.query(Conversation, Company).join(Company, Conversation.company_id == Company.id)
    if user_company_id:
        q = q.filter(Conversation.company_id == _uuid.UUID(user_company_id))

    convs = q.order_by(Conversation.last_activity_at.desc()).all()

    # Bulk-fetch active (in_progress) service count per conversation — concurrency is
    # unlimited, so several services can be in_progress at once for one conversation.
    conv_ids = [str(c.id) for c, _ in convs]
    active_status: dict[str, int] = {}
    if conv_ids:
        active_svcs = (
            db.query(Service.conversation_id)
            .join(MobileQueue, MobileQueue.service_id == Service.id)
            .filter(
                Service.conversation_id.in_([_uuid.UUID(x) for x in conv_ids]),
                MobileQueue.status == "in_progress",
            )
            .all()
        )
        for (conv_id,) in active_svcs:
            active_status[str(conv_id)] = active_status.get(str(conv_id), 0) + 1

    rows = []
    for conv, company in convs:
        rows.append({
            "id":               str(conv.id),
            "mobile_no":        conv.mobile_no,
            "company_name":     company.name if company else "—",
            "total_messages":   str(conv.total_messages or 0),
            "last_activity_at": (
                conv.last_activity_at.strftime("%d %b %Y  %H:%M")
                if conv.last_activity_at else "—"
            ),
            "active_service":   (
                f"{active_status[str(conv.id)]} active" if active_status.get(str(conv.id)) else "—"
            ),
        })

    return templates.TemplateResponse("layouts/list_view.html", {
        "request": request,
        "user":    ctx["user"],
        "perms":   ctx["perms"],
        "dt":      CONVERSATIONS_DOCTYPE,
        "rows":    rows,
        "active":  "conversations",
    })


# ── Detail ────────────────────────────────────────────────────────────────────

@router.get("/conversations/{conv_id}", response_class=HTMLResponse)
def conversation_detail(
    conv_id: str,
    request: Request,
    ctx=Depends(get_page_user),
    db: Session = Depends(get_db),
):
    if not ctx["perms"].get("conversations", {}).get("read"):
        return HTMLResponse("<h2>Access denied</h2>", status_code=403)

    try:
        oid = _uuid.UUID(conv_id)
    except ValueError:
        return HTMLResponse("<h2>Not found</h2>", status_code=404)

    conv = db.query(Conversation).filter(Conversation.id == oid).first()
    if not conv:
        return HTMLResponse("<h2>Conversation not found</h2>", status_code=404)

    user_company_id = ctx["user"].get("company_id")
    if user_company_id and str(conv.company_id) != user_company_id:
        return HTMLResponse("<h2>Access denied</h2>", status_code=403)

    company  = db.query(Company).filter(Company.id == conv.company_id).first()
    messages = (
        db.query(Message)
        .filter(Message.conversation_id == oid)
        .order_by(Message.created_at)
        .all()
    )
    services = (
        db.query(Service)
        .filter(Service.conversation_id == oid)
        .order_by(Service.created_at)
        .all()
    )
    for svc in services:
        svc._responses = (
            db.query(ServiceResponse)
            .filter(ServiceResponse.service_id == svc.id)
            .order_by(ServiceResponse.sequence)
            .all()
        )

    return templates.TemplateResponse("conversations/detail.html", {
        "request":  request,
        "user":     ctx["user"],
        "perms":    ctx["perms"],
        "active":   "conversations",
        "conv":     conv,
        "company":  company,
        "messages": messages,
        "services": services,
    })
