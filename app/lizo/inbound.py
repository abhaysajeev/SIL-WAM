"""
Handles the template button on Liso's order message.

    Confirm Order → record the confirmation and notify the client (placeholder)

conversation_engine.handle_inbound delegates here for Liso services only, via
handles(). Every other client — including the live SFA/Shirin flow — fails that
guard and runs the existing path untouched.

There was also a View Order button, which sent the itemised order as a free-form
text message because Meta rejects newlines inside template parameters. That is gone:
the order is now rendered by the client as an image and delivered as the template's
media header, so there is nothing left for a second message to show. A stray tap
from an older message falls through to the unknown-button branch and does nothing.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.lizo import notify
from app.lizo.schemas import FLOW_MARKER_KEY, FLOW_MARKER_VALUE
from app.models.conversation import Message

logger = logging.getLogger(__name__)

_CONFIRM_ORDER = "confirm_order"

# Set on Service.data once the customer confirms. Buttons stay tappable forever
# and every tap carries a fresh wamid, so handle_inbound's wamid dedup does not
# stop repeats — this marker is the only thing that makes confirmation once-only.
# Same pattern as pdf_sent in conversation_engine._handle_pdf_request.
_CONFIRMED_AT_KEY = "lizo_confirmed_at"


def handles(service) -> bool:
    """True only for services created through app/lizo/api.py."""
    return (service.data or {}).get(FLOW_MARKER_KEY) == FLOW_MARKER_VALUE


def _normalise(payload: str) -> str:
    """
    "Confirm Order" / "confirm_order" / "CONFIRM ORDER" all collapse to confirm_order.

    The template sets no explicit button payload, so Meta echoes the button label
    verbatim. Normalising means recreating the template with an explicit payload
    later will not silently break this. Mirrors _is_download_invoice.
    """
    return (payload or "").strip().lower().replace(" ", "_")


def handle_tap(
    db: Session,
    service,
    account,
    mobile_no: str,
    button_payload: str,
    inbound_msg: Message,
) -> None:
    """
    Dispatch a template button tap. Never raises.

    `account` and `mobile_no` are unused since View Order was removed, but stay on
    the signature: conversation_engine calls this positionally, and the pending
    Confirm Order callback will need both to reply to the customer.
    """
    action = _normalise(button_payload)

    if action == _CONFIRM_ORDER:
        _confirm(db, service)
    else:
        # Unknown button — including "View Order" taps on messages sent before that
        # button was dropped. Record it (handle_inbound already stored the Message)
        # and do nothing. A template can gain buttons without breaking this.
        logger.info(
            "Liso: unhandled button payload=%r service=%s", button_payload, service.service_id
        )


def _confirm(db: Session, service) -> None:
    """Record the confirmation once, then hand off to Client X."""
    data = dict(service.data or {})

    if data.get(_CONFIRMED_AT_KEY):
        logger.info(
            "Lizo: already confirmed service=%s at %s — ignoring re-tap",
            service.service_id, data[_CONFIRMED_AT_KEY],
        )
        return

    data[_CONFIRMED_AT_KEY] = datetime.now(timezone.utc).isoformat()
    service.data = data
    flag_modified(service, "data")   # JSONB mutations are invisible to SQLAlchemy otherwise

    logger.info("Liso: order confirmed service=%s", service.service_id)
    _post_confirmation(db, service)


def _post_confirmation(db: Session, service) -> None:
    """
    Tell the client the customer confirmed the order.

    Goes through app/lizo/notify.py rather than notify_queue: a Liso service is
    already "completed" by the time a button can be tapped, and notify_queue drops
    non-terminal events on a terminal service. The row it writes still rides
    notify_scheduler's retry and backoff.
    """
    confirmed_at = (service.data or {}).get(_CONFIRMED_AT_KEY)
    event_at = None
    if confirmed_at:
        try:
            event_at = datetime.fromisoformat(confirmed_at)
        except ValueError:
            pass   # fall back to now, inside notify._timestamp

    notify.emit(db, service, notify.STATUS_CONFIRMED, event_at=event_at)
    logger.info(
        "Liso: confirmation queued — service_id=%s reference_id=%s confirmed_at=%s",
        service.service_id, service.id, confirmed_at,
    )
