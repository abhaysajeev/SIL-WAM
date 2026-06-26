"""
app/services/queue_manager.py — FIFO mobile queue management.

Handles:
- Enqueueing new services (start immediately or add to back of queue)
- Condition B expiry (new service arrives while customer hasn't clicked template yet)
- Queue advancement when a service completes
- Template sending on queue activation

No FastAPI imports. No HTTPException. All failures logged, never raised.
"""
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.conversation import Message, MobileQueue, Service
from app.models.whatsapp import WhatsAppAccount, WhatsAppTemplate
from app.services import wa_sender
from app.utils.error_logger import log_error

logger = logging.getLogger(__name__)


def enqueue_service(db: Session, service: Service, account: WhatsAppAccount) -> str:
    """
    Add `service` to the mobile queue. Returns "in_progress" if the template
    was sent immediately, or "waiting" if queued behind another service.
    """
    mobile_no = _get_mobile(service)
    if not mobile_no:
        logger.error("enqueue_service: no customer_mobile in service.data id=%s", service.id)
        service.status = "failed"
        service.failed_reason = "send_error"
        return "failed"

    active = (
        db.query(MobileQueue)
        .filter(
            MobileQueue.company_id == service.company_id,
            MobileQueue.mobile_no  == mobile_no,
            MobileQueue.status.in_(["waiting", "in_progress"]),
        )
        .order_by(MobileQueue.position)
        .all()
    )

    if not active:
        _start_service(db, service, account, mobile_no, position=1)
        return "in_progress"

    # Check Condition B: in_progress with zero questions answered → expire and replace
    ip_entry = next((e for e in active if e.status == "in_progress"), None)
    if ip_entry:
        ip_svc = db.query(Service).filter(Service.id == ip_entry.service_id).first()
        questions_started = ip_svc and any(
            q.get("sent") == 1 for q in (ip_svc.questions or [])
        )
        if not questions_started:
            _expire_entries(db, active, reason="new_order_arrived")
            _start_service(db, service, account, mobile_no, position=1)
            return "in_progress"

    # Normal: queue behind existing activity
    max_pos = max(e.position for e in active)
    db.add(MobileQueue(
        company_id = service.company_id,
        mobile_no  = mobile_no,
        service_id = service.id,
        position   = max_pos + 1,
        status     = "waiting",
    ))
    return "waiting"


def advance_queue(
    db: Session,
    mobile_no: str,
    company_id: uuid.UUID,
    account: WhatsAppAccount,
) -> None:
    """Start the next waiting service in the queue for this mobile number."""
    next_entry = (
        db.query(MobileQueue)
        .filter(
            MobileQueue.company_id == company_id,
            MobileQueue.mobile_no  == mobile_no,
            MobileQueue.status     == "waiting",
        )
        .order_by(MobileQueue.position)
        .first()
    )
    if not next_entry:
        return

    next_svc = db.query(Service).filter(Service.id == next_entry.service_id).first()
    if not next_svc:
        return

    next_entry.status  = "in_progress"
    next_svc.status    = "in_progress"
    _send_template_for_service(db, next_svc, account, mobile_no)


# ── Private helpers ───────────────────────────────────────────────────────────

def _start_service(
    db: Session,
    service: Service,
    account: WhatsAppAccount,
    mobile_no: str,
    position: int,
) -> None:
    db.add(MobileQueue(
        company_id = service.company_id,
        mobile_no  = mobile_no,
        service_id = service.id,
        position   = position,
        status     = "in_progress",
    ))
    service.status = "in_progress"
    _send_template_for_service(db, service, account, mobile_no)


def _send_template_for_service(
    db: Session,
    service: Service,
    account: WhatsAppAccount,
    mobile_no: str,
) -> None:
    """Load the WhatsApp template and send it. Updates service status on failure."""
    template = db.query(WhatsAppTemplate).filter(
        WhatsAppTemplate.id == service.template_id
    ).first()

    if not template:
        logger.error("_send_template_for_service: template not found id=%s", service.template_id)
        service.status = "failed"
        service.failed_reason = "send_error"
        _mark_queue_completed(db, service)
        return

    result = wa_sender.send_template(
        account,
        template,
        service.template_params or [],
        mobile_no,
        service.cta_urls,
    )

    if result.ok:
        db.add(Message(
            conversation_id = service.conversation_id,
            service_id      = service.id,
            wamid           = result.meta_message_id,
            direction       = "outbound",
            message_type    = "template",
            content         = {"template_name": template.name},
            is_flow_message = True,
            status          = "sent",
            sent_at         = datetime.now(timezone.utc),
        ))
        # Template-only service (no questions) → complete immediately
        if not service.questions:
            service.status       = "completed"
            service.completed_at = datetime.now(timezone.utc)
            _mark_queue_completed(db, service)
    else:
        err = result.error or ""
        if "131026" in err:
            service.failed_reason = "whatsapp_number_invalid"
            logger.warning(
                "Invalid WhatsApp number mobile=%s service=%s", mobile_no, service.service_id
            )
        else:
            service.failed_reason = "send_error"
            log_error(
                f"Template send failed for service {service.service_id}",
                f"queue_manager._send_template_for_service → {mobile_no}",
                Exception(err),
            )
        service.status = "failed"
        _mark_queue_completed(db, service)


def _mark_queue_completed(db: Session, service: Service) -> None:
    """Mark the MobileQueue entry for this service as completed."""
    entry = db.query(MobileQueue).filter(
        MobileQueue.service_id == service.id,
        MobileQueue.status.in_(["waiting", "in_progress"]),
    ).first()
    if entry:
        entry.status = "completed"


def _expire_entries(db: Session, entries: list, reason: str) -> None:
    """Expire all queue entries and their services."""
    for entry in entries:
        svc = db.query(Service).filter(Service.id == entry.service_id).first()
        if svc:
            svc.status         = "expired"
            svc.expired_reason = reason
        entry.status = "completed"


def _get_mobile(service: Service) -> str | None:
    return (service.data or {}).get("customer_mobile")
