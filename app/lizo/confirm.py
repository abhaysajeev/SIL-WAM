"""
The confirmation step between the order message and the order being approved.

Tapping Confirm on the order template no longer approves anything. It sends a second
template — Confirm Order / Cancel — and only the tap on *that* decides:

    liso_with_image ──Confirm──► order_confirm_liso ──Confirm──► ApproveOrder + status
                                                     └─Cancel──► acknowledgement only

The step exists because the order template's button stays live indefinitely, so a
single accidental tap would approve an order with no way back.

This module owns the two outbound actions. Deciding *which* action belongs to a tap is
inbound.py's job.

Both acknowledgements are free-form text, which is legal because the tap itself is an
inbound message and opens Meta's 24-hour customer service window.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core import mapping
from app.core.config import settings
from app.models.conversation import Message
from app.models.whatsapp import WhatsAppTemplate
from app.services import wa_sender

logger = logging.getLogger(__name__)

ACK_CONFIRMED = "Thank you for confirming your order. We appreciate your business."

ACK_CANCELLED = (
    "Your Order #{order_no} has been cancelled as requested.\n"
    "If this was not intended, please contact us for assistance."
)


def send_confirm_template(db: Session, service, account, mobile_no: str) -> bool:
    """
    Send the Confirm Order / Cancel template. True if it went out.

    The caller stamps its "already sent" marker only on True, so a failure here leaves
    the customer able to tap again and retry rather than stranding the order.

    Writing the outbound Message with content={"template_name": ...} is not
    bookkeeping: the customer's next tap arrives carrying context.id — the wamid of
    this message — and inbound.py reads the template name back off it to tell which
    of the two "Confirm Order" buttons was pressed. Without this row the tap cannot
    be attributed and the order can never be approved.
    """
    template_name = settings.LIZO_CONFIRM_TEMPLATE
    if not template_name:
        logger.error(
            "Liso confirm: LIZO_CONFIRM_TEMPLATE is not set — cannot send the confirmation "
            "step for service=%s", service.service_id,
        )
        return False

    template = db.query(WhatsAppTemplate).filter(
        WhatsAppTemplate.name       == template_name,
        WhatsAppTemplate.company_id == service.company_id,
        WhatsAppTemplate.status     == "APPROVED",
    ).first()
    if not template:
        logger.error(
            "Liso confirm: approved template %r not found for company=%s (service=%s)",
            template_name, service.company_id, service.service_id,
        )
        return False

    # Resolved from the template's own mapping against the order data we already hold,
    # exactly as the ingest path resolves the order template. A template with no
    # placeholders needs no mapping and sends no params.
    ctx = {"data": service.data or {}, "service_id": service.service_id}
    params = mapping.resolve_params(ctx, template.param_mapping or {})

    result = wa_sender.send_template(account, template, params, mobile_no)
    if not result.ok:
        logger.error(
            "Liso confirm: send failed service=%s template=%s error=%s",
            service.service_id, template_name, result.error,
        )
        return False

    db.add(Message(
        conversation_id = service.conversation_id,
        service_id      = service.id,
        wamid           = result.meta_message_id,
        direction       = "outbound",
        message_type    = "template",
        content         = {"template_name": template.name},
        # Not part of a question flow. True would also flip the already_engaged probe
        # in handle_inbound for this service.
        is_flow_message = False,
        status          = "sent",
        sent_at         = datetime.now(timezone.utc),
    ))
    logger.info(
        "Liso confirm: step sent service=%s template=%s wamid=%s",
        service.service_id, template_name, result.meta_message_id,
    )
    return True


def send_acknowledgement(db: Session, service, account, mobile_no: str, body: str) -> None:
    """
    Reply to the customer's decision. Never raises.

    A failed acknowledgement must not undo the decision it acknowledges — the order is
    approved or cancelled either way, and the customer can be told again by hand.
    """
    result = wa_sender.send_text(account, mobile_no, body)
    if not result.ok:
        logger.error(
            "Liso confirm: acknowledgement failed service=%s error=%s",
            service.service_id, result.error,
        )
        return

    db.add(Message(
        conversation_id = service.conversation_id,
        service_id      = service.id,
        wamid           = result.meta_message_id,
        direction       = "outbound",
        message_type    = "text",
        content         = {"body": body},
        is_flow_message = False,
        status          = "sent",
        sent_at         = datetime.now(timezone.utc),
    ))


def cancelled_text(service) -> str:
    """The cancellation acknowledgement, carrying the client's own order number."""
    order_no = (service.data or {}).get("order_no", "")
    return ACK_CANCELLED.format(order_no=order_no)
