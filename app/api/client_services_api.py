"""
GET/POST /client-api/v1/services — Type B client-facing service API.

Authentication: X-API-Key header (get_api_company dependency).
No JWT / no RBAC matrix. The API key scopes all operations to the company.

Endpoints:
  POST   /client-api/v1/services                       — ingest a new service flow
  GET    /client-api/v1/services/{service_id}          — poll service status + results
  PATCH  /client-api/v1/services/{service_id}/retry    — retry after invalid WA number
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_api_company, get_api_key_and_company
from app.models.company import Company
from app.models.conversation import Conversation, MobileQueue, Service, ServiceResponse
from app.models.whatsapp import WhatsAppAccount, WhatsAppTemplate
from app.schemas.service import (
    ServiceGetResponse,
    ServiceIngestRequest,
    ServiceIngestResponse,
    ServiceResponseItem,
    ServiceRetryRequest,
)
from app.services import queue_manager
from app.utils.error_logger import log_error

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/client-api/v1", tags=["Client API"])


def _get_nested(data: dict, dot_path: str) -> str:
    """Resolve a dot-path like 'order.amount' from data dict. Returns '' if missing."""
    parts = dot_path.split(".")
    val = data
    for part in parts:
        if not isinstance(val, dict):
            return ""
        val = val.get(part, "")
    return str(val) if val is not None else ""


def _resolve_params(data: dict, param_mapping: dict) -> list[str]:
    """Build ordered template_params list from mapping. Keys are 1-indexed strings."""
    if not param_mapping:
        return []
    max_idx = max(int(k) for k in param_mapping.keys())
    result = []
    for i in range(1, max_idx + 1):
        key = str(i)
        dot_path = param_mapping.get(key, "")
        result.append(_get_nested(data, dot_path) if dot_path else "")
    return result


def _resolve_cta_urls(data: dict, cta_mapping: dict) -> dict[str, str]:
    """Build cta_urls dict from mapping. Keys are 0-indexed button position strings."""
    if not cta_mapping:
        return {}
    return {btn_idx: _get_nested(data, dot_path) for btn_idx, dot_path in cta_mapping.items()}


# ── POST /services ────────────────────────────────────────────────────────────

@router.post("/services", response_model=ServiceIngestResponse, status_code=201)
def ingest_service(
    payload: ServiceIngestRequest,
    api_key_company: tuple = Depends(get_api_key_and_company),
    db: Session = Depends(get_db),
):
    api_key, company = api_key_company

    # 1. Duplicate check — service_id is unique per company
    if db.query(Service).filter(
        Service.service_id == payload.service_id,
        Service.company_id == company.id,
    ).first():
        raise HTTPException(409, f"service_id '{payload.service_id}' already exists for this company")

    # 2. Template must exist, belong to this company, and be approved
    template = db.query(WhatsAppTemplate).filter(
        WhatsAppTemplate.name       == payload.template_name,
        WhatsAppTemplate.company_id == company.id,
        WhatsAppTemplate.status     == "APPROVED",
    ).first()
    if not template:
        raise HTTPException(404, f"Approved template '{payload.template_name}' not found for this company")

    # 3. WhatsApp account must be configured
    account = db.query(WhatsAppAccount).filter(
        WhatsAppAccount.company_id == company.id
    ).first()
    if not account:
        raise HTTPException(503, "WhatsApp account not configured for this company")

    # 4. Extract and separate questions from opaque data
    questions    = payload.data.get("questions") or []
    service_data = {k: v for k, v in payload.data.items() if k != "questions"}

    # Client's answer_type convention is 1-indexed (1=yes/no, 2=rating, 3=free text);
    # our internal engine is 0-indexed (0=yes/no, 1=rating, 2=free text). Translate at
    # this boundary so conversation_engine's dispatch logic never needs to know about
    # the client's numbering. Mirrored on the way out in notify_queue._build_payload.
    for q in questions:
        if "answer_type" in q:
            q["answer_type"] = q["answer_type"] - 1

    # Validate question field_keys are unique within this service
    if questions:
        field_keys = [q.get("field_key") for q in questions if q.get("field_key")]
        if len(field_keys) != len(set(field_keys)):
            raise HTTPException(400, "Duplicate field_key found in questions — each field_key must be unique")
        bad = [q.get("field_key") for q in questions if q.get("answer_type") not in (0, 1, 2)]
        if bad:
            raise HTTPException(
                400,
                f"Invalid answer_type for field_key(s) {bad} — must be 1 (yes/no), 2 (rating), or 3 (free text)",
            )

    # Full resolver context — dot-paths are relative to the full payload envelope
    # so "data.customer_name" resolves correctly from {"data": {...}, "service_id": ...}
    resolver_ctx = {"data": service_data, "service_id": payload.service_id}

    # Resolve phone number — use mobile_mapping dot-path if configured, else fall back to data.customer_mobile
    if template.mobile_mapping:
        customer_mobile = _get_nested(resolver_ctx, template.mobile_mapping)
    else:
        customer_mobile = str(service_data.get("customer_mobile", ""))

    service_data["customer_mobile"] = customer_mobile

    # Auto-resolve template_params and cta_urls from template mapping when not supplied
    template_params = payload.template_params or []
    cta_urls        = payload.cta_urls
    if not template_params and template.param_mapping:
        template_params = _resolve_params(resolver_ctx, template.param_mapping)
    if cta_urls is None and template.cta_mapping:
        cta_urls = _resolve_cta_urls(resolver_ctx, template.cta_mapping)

    # 5. Get or create Conversation
    conv = db.query(Conversation).filter(
        Conversation.company_id == company.id,
        Conversation.mobile_no  == customer_mobile,
    ).first()
    if not conv:
        conv = Conversation(company_id=company.id, mobile_no=customer_mobile)
        db.add(conv)
        db.flush()

    # 6. Create Service
    service = Service(
        conversation_id       = conv.id,
        company_id            = company.id,
        api_key_id            = api_key.id,
        service_id            = payload.service_id,
        template_id           = template.id,
        template_params       = template_params,
        cta_urls              = cta_urls,
        template_expiry_hours = payload.template_expiry_hours,
        questions             = questions if questions else None,
        data                  = service_data,
        status                = "waiting",
    )
    db.add(service)
    db.flush()

    # 7. Enqueue (may send template immediately)
    try:
        queue_status = queue_manager.enqueue_service(db, service, account)
        db.commit()
    except Exception as exc:
        db.rollback()
        log_error(
            f"Service ingest failed service_id={payload.service_id}",
            "POST /client-api/v1/services",
            exc,
        )
        raise HTTPException(500, "Internal error during service creation")

    return ServiceIngestResponse(
        service_id=payload.service_id,
        reference_id=service.id,
        status=queue_status,
    )


# ── GET /services/{service_id} ────────────────────────────────────────────────

@router.get("/services/{service_id}", response_model=ServiceGetResponse)
def get_service(
    service_id: str,
    company: Company = Depends(get_api_company),
    db: Session = Depends(get_db),
):
    service = db.query(Service).filter(
        Service.service_id == service_id,
        Service.company_id == company.id,
    ).first()
    if not service:
        raise HTTPException(404, f"service_id '{service_id}' not found")

    responses = (
        db.query(ServiceResponse)
        .filter(ServiceResponse.service_id == service.id)
        .order_by(ServiceResponse.sequence)
        .all()
    )

    questions = service.questions or []
    completed_count = sum(1 for q in questions if q.get("sent") == 1)

    return ServiceGetResponse(
        service_id          = service.service_id,
        status              = service.status,
        failed_reason       = service.failed_reason,
        expired_reason      = service.expired_reason,
        completed_questions = completed_count,
        total_questions     = len(questions),
        data                = service.data,
        responses           = [ServiceResponseItem.model_validate(r) for r in responses],
        created_at          = service.created_at,
        completed_at        = service.completed_at,
    )


# ── PATCH /services/{service_id}/retry ───────────────────────────────────────

@router.patch("/services/{service_id}/retry", response_model=ServiceIngestResponse)
def retry_service(
    service_id: str,
    payload: ServiceRetryRequest,
    company: Company = Depends(get_api_company),
    db: Session = Depends(get_db),
):
    service = db.query(Service).filter(
        Service.service_id == service_id,
        Service.company_id == company.id,
    ).first()
    if not service:
        raise HTTPException(404, f"service_id '{service_id}' not found")

    if service.status != "failed" or service.failed_reason != "whatsapp_number_invalid":
        raise HTTPException(
            409,
            "Retry is only allowed for services that failed with 'whatsapp_number_invalid'. "
            f"Current status: {service.status}, reason: {service.failed_reason}",
        )

    account = db.query(WhatsAppAccount).filter(
        WhatsAppAccount.company_id == company.id
    ).first()
    if not account:
        raise HTTPException(503, "WhatsApp account not configured for this company")

    # Update mobile number and reset service state
    new_mobile = payload.customer_mobile
    data = dict(service.data or {})
    data["customer_mobile"] = new_mobile
    service.data         = data
    service.status        = "waiting"
    service.failed_reason = None
    service.template_sent = False

    # Update or create conversation for new mobile
    conv = db.query(Conversation).filter(
        Conversation.company_id == company.id,
        Conversation.mobile_no  == new_mobile,
    ).first()
    if not conv:
        conv = Conversation(company_id=company.id, mobile_no=new_mobile)
        db.add(conv)
        db.flush()
    service.conversation_id = conv.id

    db.flush()

    try:
        queue_status = queue_manager.enqueue_service(db, service, account)
        db.commit()
    except Exception as exc:
        db.rollback()
        log_error(
            f"Service retry failed service_id={service_id}",
            "PATCH /client-api/v1/services/{service_id}/retry",
            exc,
        )
        raise HTTPException(500, "Internal error during retry")

    return ServiceIngestResponse(
        service_id=service_id,
        reference_id=service.id,
        status=queue_status,
    )
