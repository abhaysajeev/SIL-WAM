"""Services — list + detail page routes (read-only; services created via client API)."""
import os
import uuid as _uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_page_user
from app.core.doctypes import SERVICES_DOCTYPE
from app.models.company import Company
from app.models.conversation import Conversation, Service, ServiceResponse
from app.models.whatsapp import WhatsAppTemplate

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

if "dv_search_text" not in templates.env.filters:
    templates.env.filters["dv_search_text"] = (
        lambda r: " ".join(str(v) for v in r.values() if v is not None).lower()
    )

router = APIRouter(tags=["Pages"])

_ANSWER_TYPE_LABELS = {0: "Yes / No", 1: "Rating", 2: "Free Text"}


def _service_row(svc: Service, company: Company) -> dict:
    questions = svc.questions or []
    answered  = sum(1 for q in questions if q.get("sent") == 1)
    total     = len(questions)
    return {
        "id":           str(svc.id),
        "service_id":   svc.service_id,
        "company_name": company.name if company else "—",
        "status":       svc.status,
        "progress":     f"{answered} / {total} questions" if total else "Template only",
        "created_at":   svc.created_at.strftime("%d %b %Y  %H:%M") if svc.created_at else "—",
        "completed_at": svc.completed_at.strftime("%d %b %Y  %H:%M") if svc.completed_at else "—",
    }


# ── List ─────────────────────────────────────────────────────────────────────

@router.get("/services", response_class=HTMLResponse)
def services_list(
    request: Request,
    ctx=Depends(get_page_user),
    db: Session = Depends(get_db),
):
    if not ctx["perms"].get("services", {}).get("read"):
        return HTMLResponse("<h2>Access denied</h2>", status_code=403)

    user_company_id = ctx["user"].get("company_id")
    status_filter   = request.query_params.get("status", "")

    q = db.query(Service, Company).join(Company, Service.company_id == Company.id)
    if user_company_id:
        q = q.filter(Service.company_id == _uuid.UUID(user_company_id))
    if status_filter:
        q = q.filter(Service.status == status_filter)

    rows = [_service_row(svc, cmp) for svc, cmp in q.order_by(Service.created_at.desc()).all()]

    return templates.TemplateResponse("layouts/list_view.html", {
        "request":       request,
        "user":          ctx["user"],
        "perms":         ctx["perms"],
        "dt":            SERVICES_DOCTYPE,
        "rows":          rows,
        "active":        "services",
        "status_filter": status_filter,
    })


# ── Detail ────────────────────────────────────────────────────────────────────

@router.get("/services/{service_id}", response_class=HTMLResponse)
def service_detail(
    service_id: str,
    request: Request,
    ctx=Depends(get_page_user),
    db: Session = Depends(get_db),
):
    if not ctx["perms"].get("services", {}).get("read"):
        return HTMLResponse("<h2>Access denied</h2>", status_code=403)

    try:
        oid = _uuid.UUID(service_id)
    except ValueError:
        return HTMLResponse("<h2>Not found</h2>", status_code=404)

    svc = db.query(Service).filter(Service.id == oid).first()
    if not svc:
        return HTMLResponse("<h2>Service not found</h2>", status_code=404)

    user_company_id = ctx["user"].get("company_id")
    if user_company_id and str(svc.company_id) != user_company_id:
        return HTMLResponse("<h2>Access denied</h2>", status_code=403)

    company  = db.query(Company).filter(Company.id == svc.company_id).first()
    conv     = db.query(Conversation).filter(Conversation.id == svc.conversation_id).first()
    template = db.query(WhatsAppTemplate).filter(WhatsAppTemplate.id == svc.template_id).first()
    responses = (
        db.query(ServiceResponse)
        .filter(ServiceResponse.service_id == svc.id)
        .order_by(ServiceResponse.sequence)
        .all()
    )

    # Merge questions + responses into a single display list
    questions    = svc.questions or []
    resp_by_seq  = {r.sequence: r for r in responses}
    qa_rows = []
    for q in sorted(questions, key=lambda x: x.get("sequence", 0)):
        seq  = q.get("sequence")
        resp = resp_by_seq.get(seq)
        qa_rows.append({
            "sequence":      seq,
            "field_key":     q.get("field_key", "—"),
            "question":      q.get("question", "—"),
            "answer_type":   _ANSWER_TYPE_LABELS.get(q.get("answer_type"), "?"),
            "sent":          q.get("sent", 0),
            "response":      resp.response_value if resp else None,
            "responded_at":  resp.responded_at.strftime("%d %b %Y  %H:%M") if resp and resp.responded_at else "—",
        })

    return templates.TemplateResponse("services/detail.html", {
        "request":   request,
        "user":      ctx["user"],
        "perms":     ctx["perms"],
        "active":    "services",
        "svc":       svc,
        "company":   company,
        "conv":      conv,
        "template":  template,
        "qa_rows":   qa_rows,
    })
