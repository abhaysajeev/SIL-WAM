"""
app/services/queue_manager.py — per-mobile service activation.

Handles:
- Enqueueing new services (every service starts immediately — concurrency is
  unlimited, no pre-emption, no waiting queue)
- Template sending on activation

No FastAPI imports. No HTTPException. All failures logged, never raised.
"""
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.conversation import Message, MobileQueue, Service
from app.models.whatsapp import WhatsAppAccount, WhatsAppTemplate
from app.services import notify_queue, wa_sender
from app.utils.error_logger import log_error

logger = logging.getLogger(__name__)


def enqueue_service(db: Session, service: Service, account: WhatsAppAccount) -> str:
    """
    Activate `service` immediately. Concurrency is unlimited — multiple services can
    be "in_progress" for the same mobile number at once, distinguished by the order
    number shown on each message (see conversation_engine's footer/body injection).
    Positioning only — the actual Meta template send is picked up asynchronously by
    send_scheduler (template_sent stays False here).
    """
    mobile_no = _get_mobile(service)
    if not mobile_no:
        logger.error("enqueue_service: no customer_mobile in service.data id=%s", service.id)
        service.status = "failed"
        service.failed_reason = "send_error"
        notify_queue.enqueue_notification(db, service, "failed", note="send_error")
        return "failed"

    _start_service(db, service, account, mobile_no, position=1)
    return "in_progress"


def advance_queue(
    db: Session,
    mobile_no: str,
    company_id: uuid.UUID,
    account: WhatsAppAccount,
) -> None:
    """
    Activate the next waiting service in the queue for this mobile number.
    Only flips status — the actual template send is picked up asynchronously
    by send_scheduler (template_sent stays False here).
    """
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


def send_template_for_service(
    db: Session,
    service: Service,
    account: WhatsAppAccount,
) -> None:
    """
    Load the WhatsApp template and send it. Updates service status on failure.

    Called by send_scheduler, not the request path — this is the one place that
    actually talks to the Meta Graph API. template_sent is set True unconditionally
    up front so a claimed row is never retried automatically, regardless of outcome.
    """
    service.template_sent = True

    mobile_no = _get_mobile(service)
    if not mobile_no:
        logger.error("send_template_for_service: no customer_mobile in service.data id=%s", service.id)
        service.status = "failed"
        service.failed_reason = "send_error"
        _mark_queue_completed(db, service)
        notify_queue.enqueue_notification(db, service, "failed", note="send_error")
        _release_free_text(db, service, account)
        return

    template = db.query(WhatsAppTemplate).filter(
        WhatsAppTemplate.id == service.template_id
    ).first()

    if not template:
        logger.error("send_template_for_service: template not found id=%s", service.template_id)
        service.status = "failed"
        service.failed_reason = "send_error"
        _mark_queue_completed(db, service)
        notify_queue.enqueue_notification(db, service, "failed", note="send_error")
        _release_free_text(db, service, account)
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
            notify_queue.enqueue_notification(db, service, "completed")
            _release_free_text(db, service, account)
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
                f"queue_manager.send_template_for_service → {mobile_no}",
                Exception(err),
            )
        service.status = "failed"
        _mark_queue_completed(db, service)
        notify_queue.enqueue_notification(db, service, "failed", note=service.failed_reason)
        _release_free_text(db, service, account)


def _mark_queue_completed(db: Session, service: Service) -> None:
    """Mark the MobileQueue entry for this service as completed."""
    entry = db.query(MobileQueue).filter(
        MobileQueue.service_id == service.id,
        MobileQueue.status.in_(["waiting", "in_progress"]),
    ).first()
    if entry:
        entry.status = "completed"


def _release_free_text(db: Session, service: Service, account: WhatsAppAccount) -> None:
    """
    Best-effort: this service just reached a terminal state (completed/failed).
    If another concurrent service on the same mobile has a free-text question held
    back (see conversation_engine._has_outstanding_free_text), fire it now. Local
    import avoids a circular dependency (conversation_engine imports this module).
    """
    try:
        from app.services import conversation_engine
        conversation_engine._release_free_text_slot(db, account, service.conversation_id)
    except Exception as exc:
        log_error(
            f"_release_free_text failed for service={service.service_id}",
            "queue_manager._release_free_text",
            exc,
        )


def _get_mobile(service: Service) -> str | None:
    return (service.data or {}).get("customer_mobile")
