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
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models.conversation import Message, MobileQueue, Service
from app.models.whatsapp import WhatsAppAccount, WhatsAppTemplate
from app.services import notify_queue, wa_sender
from app.utils.error_logger import log_error

logger = logging.getLogger(__name__)

# send_error is retried this many times total (1 original attempt + retries below)
# before becoming terminal. whatsapp_number_invalid never retries — see
# _fail_or_schedule_retry. Backoff is short (30s, then 2min) because a real customer
# may be waiting mid-flow, unlike notify_scheduler's webhook retries which can wait
# up to an hour with no UX cost.
_MAX_SEND_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = [30, 120]  # index = attempts made so far, 0-indexed


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
    up front so a claimed row is never picked up twice concurrently; a send_error
    failure resets it back to False (with next_retry_at set) to re-enter the claim
    pool for a bounded number of retries — see _fail_or_schedule_retry.
    """
    service.template_sent = True
    service.send_attempts += 1

    mobile_no = _get_mobile(service)
    if not mobile_no:
        logger.error("send_template_for_service: no customer_mobile in service.data id=%s", service.id)
        _fail_or_schedule_retry(db, service, "send_error")
        return

    template = db.query(WhatsAppTemplate).filter(
        WhatsAppTemplate.id == service.template_id
    ).first()

    if not template:
        logger.error("send_template_for_service: template not found id=%s", service.template_id)
        _fail_or_schedule_retry(db, service, "send_error")
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
    else:
        err = result.error or ""
        if "131026" in err:
            # Permanent — retrying the same number would just fail identically every
            # time. Client must submit a corrected number via the retry endpoint.
            logger.warning(
                "Invalid WhatsApp number mobile=%s service=%s", mobile_no, service.service_id
            )
            service.failed_reason = "whatsapp_number_invalid"
            service.status = "failed"
            _mark_queue_completed(db, service)
            notify_queue.enqueue_notification(db, service, "failed", note=service.failed_reason)
        else:
            log_error(
                f"Template send failed for service {service.service_id}",
                f"queue_manager.send_template_for_service → {mobile_no}",
                Exception(err),
            )
            _fail_or_schedule_retry(db, service, "send_error")


def _fail_or_schedule_retry(db: Session, service: Service, reason: str) -> None:
    """
    Called for a retryable (send_error) failure. Schedules another attempt if the
    cap hasn't been reached yet; otherwise finalizes the service as terminally
    failed, same as a non-retryable (whatsapp_number_invalid) failure.

    service.send_attempts is incremented once per real attempt at the top of
    send_template_for_service, before any failure branch is reached.
    """
    service.failed_reason = reason
    if service.send_attempts < _MAX_SEND_ATTEMPTS:
        delay = _RETRY_BACKOFF_SECONDS[service.send_attempts - 1]
        service.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
        service.template_sent = False  # re-enter send_scheduler's claim pool
        logger.info(
            "Scheduling retry %d/%d for service=%s in %ds",
            service.send_attempts, _MAX_SEND_ATTEMPTS, service.service_id, delay,
        )
        # status stays "in_progress" — not done yet, don't touch the queue entry.
    else:
        service.status = "failed"
        _mark_queue_completed(db, service)
        notify_queue.enqueue_notification(db, service, "failed", note=reason)


def _mark_queue_completed(db: Session, service: Service) -> None:
    """Mark the MobileQueue entry for this service as completed."""
    entry = db.query(MobileQueue).filter(
        MobileQueue.service_id == service.id,
        MobileQueue.status.in_(["waiting", "in_progress"]),
    ).first()
    if entry:
        entry.status = "completed"


def _get_mobile(service: Service) -> str | None:
    return (service.data or {}).get("customer_mobile")
