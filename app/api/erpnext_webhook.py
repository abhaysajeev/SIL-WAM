"""
POST /webhook/erpnext/notify — receive template-send requests from ERPNext hooks.

Used by:
  - ERPNext Payment Entry server-side hook (payment receipt notification)
  - ERPNext Sales Invoice WhatsApp button (invoice notification with PDF button)

Auth:  X-API-Key header (same get_api_company dep as client API).
Flow:  validate → look up template by name → look up WA account →
       get/create Conversation → create Service → enqueue (sends template immediately,
       completes at once since no questions) → return 200.

Storing a Service record (even with no questions) ensures:
  - Full audit trail in the messages table
  - invoice_no is persisted in service.data so the PDF flow can retrieve it later
  - Retry-safe: duplicate reference_id returns 200 without re-sending

PDF pre-fetch optimisation:
  When invoice_no is present, a BackgroundTask immediately fetches the PDF from ERPNext
  and uploads it to Meta after the template is sent. The resulting media_id is stored in
  service.data["pdf_media_id"]. On button tap, _handle_pdf_request uses the cached
  media_id directly (one Meta API call) instead of fetching + uploading on demand
  (three network round-trips). Reduces tap-to-delivery from ~10s to ~1-2s.
"""
import logging
import uuid as _uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.core.database import SessionLocal, get_db
from app.core.deps import get_api_company
from app.models.conversation import Conversation, Service
from app.models.company import Company
from app.models.erpnext_config import ERPNextConfig
from app.models.whatsapp import WhatsAppAccount, WhatsAppTemplate
from app.services import erpnext_client, queue_manager
from app.utils.error_logger import log_error

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook/erpnext", tags=["ERPNext Webhook"])


# ── Schema ────────────────────────────────────────────────────────────────────

class ERPNextNotifyRequest(BaseModel):
    customer_mobile: str
    template_name:   str
    template_params: list[str] = []
    # Optional client reference — used as service_id for dedup and audit trail.
    # If omitted a UUID is auto-generated.
    reference_id:    str | None = None
    # If present, stored in service.data so the PDF download flow can retrieve it.
    invoice_no:      str | None = None
    # Any extra fields the ERPNext hook wants to preserve (opaque blob).
    extra_data:      dict[str, Any] | None = None


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.post("/notify")
def erpnext_notify(
    payload:          ERPNextNotifyRequest,
    background_tasks: BackgroundTasks,
    company:          Company = Depends(get_api_company),
    db:               Session = Depends(get_db),
):
    """
    Receive a template-send request from an ERPNext server-side hook.

    Returns 200 immediately. Template delivery is synchronous within this request
    (unlike the Meta inbound webhook which uses BackgroundTasks) because ERPNext
    hooks expect a confirmation response and the send is fast.
    """
    # ── 1. Dedup on reference_id ─────────────────────────────────────────────
    service_id = payload.reference_id or f"erp-{_uuid.uuid4()}"
    existing = db.query(Service).filter(Service.service_id == service_id).first()
    if existing:
        logger.info("erpnext_notify: duplicate reference_id=%s — skipping", service_id)
        return {"status": "ok", "note": "duplicate — already processed"}

    # ── 2. Look up template by name + company ─────────────────────────────────
    template = db.query(WhatsAppTemplate).filter(
        WhatsAppTemplate.company_id == company.id,
        WhatsAppTemplate.name       == payload.template_name,
        WhatsAppTemplate.status     == "APPROVED",
    ).first()
    if not template:
        raise HTTPException(
            404,
            f"Template '{payload.template_name}' not found or not approved for this company.",
        )

    # ── 3. WhatsApp account ───────────────────────────────────────────────────
    account = db.query(WhatsAppAccount).filter(
        WhatsAppAccount.company_id == company.id
    ).first()
    if not account or not account.access_token_encrypted:
        raise HTTPException(503, "WhatsApp account not configured for this company.")

    # ── 4. Get/create Conversation ────────────────────────────────────────────
    conv = db.query(Conversation).filter(
        Conversation.company_id == company.id,
        Conversation.mobile_no  == payload.customer_mobile,
    ).first()
    if not conv:
        conv = Conversation(company_id=company.id, mobile_no=payload.customer_mobile)
        db.add(conv)
        db.flush()

    # ── 5. Build service data payload ─────────────────────────────────────────
    service_data: dict[str, Any] = {
        "customer_mobile": payload.customer_mobile,
    }
    if payload.invoice_no:
        service_data["invoice_no"] = payload.invoice_no
    if payload.extra_data:
        service_data.update(payload.extra_data)

    # ── 6. Create Service record ──────────────────────────────────────────────
    svc = Service(
        conversation_id       = conv.id,
        company_id            = company.id,
        service_id            = service_id,
        template_id           = template.id,
        template_params       = payload.template_params or [],
        cta_urls              = None,
        template_expiry_hours = 24,
        questions             = None,  # no Q&A — template-only flow
        data                  = service_data,
        status                = "waiting",
    )
    db.add(svc)
    db.flush()

    # ── 7. Enqueue (positions in queue; send_scheduler dispatches the template) ─
    try:
        queue_manager.enqueue_service(db, svc, account)
        db.commit()
    except Exception as exc:
        db.rollback()
        log_error(
            f"ERPNext notify failed for reference_id={service_id}",
            "POST /webhook/erpnext/notify",
            exc,
        )
        raise HTTPException(500, "Internal error — template send failed.")

    # ── 8. Pre-fetch PDF in background so button tap is instant ───────────────
    if payload.invoice_no:
        background_tasks.add_task(
            _prefetch_pdf_bg,
            service_db_id = str(svc.id),
            invoice_no    = payload.invoice_no,
            company_id    = str(company.id),
        )

    return {"status": "ok", "service_id": service_id}


# ── PDF pre-fetch background task ─────────────────────────────────────────────

def _prefetch_pdf_bg(service_db_id: str, invoice_no: str, company_id: str) -> None:
    """
    Fetch the invoice PDF from ERPNext and upload it to Meta immediately after
    the template is sent. Stores the resulting media_id in service.data so that
    _handle_pdf_request can skip the fetch+upload on button tap.
    """
    db = SessionLocal()
    try:
        cid = _uuid.UUID(company_id)

        config = db.query(ERPNextConfig).filter(
            ERPNextConfig.company_id == cid,
            ERPNextConfig.is_active  == True,
        ).first()
        if not config:
            return

        account = db.query(WhatsAppAccount).filter(
            WhatsAppAccount.company_id == cid
        ).first()
        if not account or not account.access_token_encrypted:
            return

        pdf_bytes = erpnext_client.fetch_invoice_pdf(config, invoice_no)
        media_id  = erpnext_client.upload_to_meta(account, pdf_bytes, f"{invoice_no}.pdf")

        svc = db.query(Service).filter(
            Service.id == _uuid.UUID(service_db_id)
        ).first()
        if svc:
            data = dict(svc.data or {})
            data["pdf_media_id"] = media_id
            svc.data = data
            flag_modified(svc, "data")
            db.commit()
            logger.info("PDF pre-fetched for invoice_no=%s media_id=%s", invoice_no, media_id)

    except Exception as exc:
        log_error(
            f"PDF pre-fetch failed for invoice_no={invoice_no}",
            "erpnext_webhook._prefetch_pdf_bg",
            exc,
        )
    finally:
        db.close()
