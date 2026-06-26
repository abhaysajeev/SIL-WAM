"""
Demo Messaging API — super_admin only, for live demo purposes.

GET  /api/demo/companies                            — list all active companies
GET  /api/demo/templates?company_id=X               — approved templates for a company
GET  /api/demo/conversation?company_id=X&mobile_no=Y — conversation messages
POST /api/demo/send                                 — send a template message
"""
import re
import uuid as _uuid
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_super_admin
from app.models.company import Company
from app.models.conversation import Conversation, Message, Service
from app.models.whatsapp import WhatsAppAccount, WhatsAppTemplate
from app.services import queue_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/demo", tags=["Demo"])


# ── Companies ─────────────────────────────────────────────────────────────────

@router.get("/companies")
def list_companies(_user=Depends(require_super_admin), db: Session = Depends(get_db)):
    rows = db.query(Company).filter(Company.is_active.is_(True)).order_by(Company.name).all()
    return [{"id": str(c.id), "name": c.name} for c in rows]


# ── Templates ─────────────────────────────────────────────────────────────────

@router.get("/templates")
def list_templates(
    company_id: str = Query(...),
    _user=Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    try:
        cid = _uuid.UUID(company_id)
    except ValueError:
        raise HTTPException(400, "Invalid company_id")

    templates = (
        db.query(WhatsAppTemplate)
        .filter(
            WhatsAppTemplate.company_id == cid,
            WhatsAppTemplate.status     == "APPROVED",
        )
        .order_by(WhatsAppTemplate.name)
        .all()
    )

    result = []
    for t in templates:
        params = _extract_params(t.components or [], t.param_mapping or {})
        body_text = _get_body_text(t.components or [])
        result.append({
            "id":         str(t.id),
            "name":       t.name,
            "language":   t.language,
            "body_text":  body_text,
            "params":     params,         # [{index, label, hint}]
        })
    return result


# ── Conversation history ──────────────────────────────────────────────────────

@router.get("/conversation")
def get_conversation(
    company_id: str = Query(...),
    mobile_no:  str = Query(...),
    _user=Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    try:
        cid = _uuid.UUID(company_id)
    except ValueError:
        raise HTTPException(400, "Invalid company_id")

    conv = db.query(Conversation).filter(
        Conversation.company_id == cid,
        Conversation.mobile_no  == mobile_no,
    ).first()

    if not conv:
        return {"conversation_id": None, "mobile_no": mobile_no, "messages": []}

    msgs = (
        db.query(Message)
        .filter(Message.conversation_id == conv.id)
        .order_by(Message.created_at.asc())
        .limit(100)
        .all()
    )

    return {
        "conversation_id": str(conv.id),
        "mobile_no":       mobile_no,
        "messages": [_fmt_message(m) for m in msgs],
    }


# ── Send template ─────────────────────────────────────────────────────────────

class DemoSendRequest(BaseModel):
    company_id:  str
    template_id: str
    mobile_no:   str
    params:      list[str] = []   # ordered values for {{1}}, {{2}}, ...


@router.post("/send")
def demo_send(
    payload: DemoSendRequest,
    _user=Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    try:
        cid = _uuid.UUID(payload.company_id)
        tid = _uuid.UUID(payload.template_id)
    except ValueError:
        raise HTTPException(400, "Invalid UUID")

    template = db.query(WhatsAppTemplate).filter(
        WhatsAppTemplate.id         == tid,
        WhatsAppTemplate.company_id == cid,
        WhatsAppTemplate.status     == "APPROVED",
    ).first()
    if not template:
        raise HTTPException(404, "Template not found or not approved")

    account = db.query(WhatsAppAccount).filter(
        WhatsAppAccount.company_id == cid
    ).first()
    if not account or not account.access_token_encrypted:
        raise HTTPException(503, "WhatsApp account not configured for this company")

    mobile = payload.mobile_no.strip().lstrip("+").replace(" ", "").replace("-", "")
    if len(mobile) == 10:
        mobile = "91" + mobile

    # Get or create conversation
    conv = db.query(Conversation).filter(
        Conversation.company_id == cid,
        Conversation.mobile_no  == mobile,
    ).first()
    if not conv:
        conv = Conversation(company_id=cid, mobile_no=mobile)
        db.add(conv)
        db.flush()

    service_id = f"demo-{_uuid.uuid4().hex[:12]}"
    svc = Service(
        conversation_id       = conv.id,
        company_id            = cid,
        service_id            = service_id,
        template_id           = template.id,
        template_params       = payload.params,
        cta_urls              = None,
        template_expiry_hours = 24,
        questions             = None,
        data                  = {"customer_mobile": mobile},
        status                = "waiting",
    )
    db.add(svc)
    db.flush()

    try:
        queue_manager.enqueue_service(db, svc, account)
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("demo send failed: %s", exc)
        raise HTTPException(500, f"Send failed: {exc}")

    return {
        "service_id":      service_id,
        "status":          svc.status,
        "conversation_id": str(conv.id),
        "mobile_no":       mobile,
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_body_text(components: list) -> str:
    body = next((c for c in components if c.get("type") == "BODY"), None)
    return body.get("text", "") if body else ""


def _extract_params(components: list, param_mapping: dict) -> list[dict]:
    """Return ordered list of param descriptors for the template body."""
    body_text = _get_body_text(components)
    indices   = sorted(set(re.findall(r"\{\{(\d+)\}\}", body_text)), key=int)
    result    = []
    for idx in indices:
        dot_path = param_mapping.get(idx, "")
        # Turn "data.customer_name" → "Customer Name"
        label = dot_path.split(".")[-1].replace("_", " ").title() if dot_path else f"Param {idx}"
        result.append({"index": idx, "label": label, "hint": dot_path})
    return result


def _fmt_message(msg: Message) -> dict:
    content  = msg.content or {}
    msg_type = msg.message_type or ""

    # Extract readable text from message content
    if msg_type == "text":
        text = content.get("body") or content.get("text", {}).get("body", "")
    elif msg_type == "template":
        text = f"[Template sent]"
    elif msg_type == "interactive":
        interactive = content.get("interactive", {})
        if "button_reply" in interactive:
            text = interactive["button_reply"].get("title", "[Button reply]")
        elif "list_reply" in interactive:
            text = interactive["list_reply"].get("title", "[List reply]")
        else:
            text = content.get("button", {}).get("text") or "[Interactive]"
    elif msg_type == "button":
        text = content.get("button", {}).get("text") or "[Button tap]"
    elif msg_type == "document":
        text = f"[Document: {content.get('filename', 'file')}]"
    else:
        # Fallback: try common content fields
        text = (
            content.get("body")
            or content.get("question")
            or content.get("text", {}).get("body", "")
            or f"[{msg_type}]"
        )

    return {
        "id":           str(msg.id),
        "direction":    msg.direction,
        "message_type": msg_type,
        "text":         text,
        "status":       msg.status,
        "created_at":   msg.created_at.isoformat() if msg.created_at else None,
    }
