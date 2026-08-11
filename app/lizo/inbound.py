"""
Handles the template buttons on Liso's order flow.

Two templates, two steps:

    liso_with_image      [Confirm Order]           → send the confirmation template
    order_confirm_liso   [Confirm Order] [Cancel]  → approve, or cancel

Confirming is deliberately two taps. The order message's button stays live
indefinitely, so a single accidental tap used to approve an order in SFA with no way
back. Nothing irreversible now happens until the second template is answered.

conversation_engine.handle_inbound delegates here for Liso services only, via
handles(). Every other client — including the live SFA/Shirin flow — fails that guard
and runs the existing path untouched.

There was also a View Order button, which sent the itemised order as a free-form text
message because Meta rejects newlines inside template parameters. It is gone: the
order is rendered by the client as an image and delivered as the template's media
header. A stray tap from an older message falls through to the unknown-button branch.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.core.config import settings
from app.lizo import approve, confirm, notify
from app.lizo.schemas import FLOW_MARKER_KEY, FLOW_MARKER_VALUE, FLOW_VALUES
from app.models.conversation import Message

logger = logging.getLogger(__name__)

_CONFIRM_ORDER = "confirm_order"
_CANCEL        = "cancel"

# Markers on Service.data. Buttons stay tappable forever and every tap carries a fresh
# wamid, so handle_inbound's wamid dedup does not stop repeats — these are the only
# thing making each step happen once. Same pattern as pdf_sent in
# conversation_engine._handle_pdf_request.
_CONFIRM_SENT_KEY = "liso_confirm_sent_at"   # the confirmation template went out
_CONFIRMED_AT_KEY = "lizo_confirmed_at"      # customer confirmed (pre-dates the two-step flow)
_CANCELLED_AT_KEY = "liso_cancelled_at"      # customer cancelled


def handles(service) -> bool:
    """
    True for any service created through app/lizo/api.py — orders and payments both.

    Deliberately claims payments too, even though nothing here acts on them. If it did
    not, a tap or reply on a payment message would fall through to the shared
    questionnaire path in conversation_engine, which fires a "responded" notification
    and tries to dispatch a next question that does not exist.
    """
    return (service.data or {}).get(FLOW_MARKER_KEY) in FLOW_VALUES


def _is_order(service) -> bool:
    """Only the order flow has buttons to act on."""
    return (service.data or {}).get(FLOW_MARKER_KEY) == FLOW_MARKER_VALUE


def _normalise(payload: str) -> str:
    """
    "Confirm Order" / "confirm_order" / "CONFIRM ORDER" all collapse to confirm_order.

    The templates set no explicit button payload, so Meta echoes the button label
    verbatim. Normalising means recreating a template with an explicit payload later
    will not silently break this. Mirrors _is_download_invoice.
    """
    return (payload or "").strip().lower().replace(" ", "_")


def _tapped_template(db: Session, inbound_msg: Message) -> str | None:
    """
    Which template the customer tapped, or None if it cannot be determined.

    Both templates carry a button labelled "Confirm Order", so the label alone cannot
    route the tap — and both resolve to the same Service. Meta's context.id is the
    wamid of the message being replied to, and conversation_engine stores the entire
    raw payload as the inbound Message's content, so the reference is already here.
    queue_manager and confirm.send_confirm_template both stamp
    content={"template_name": ...} on the outbound row this resolves to.
    """
    context_wamid = ((inbound_msg.content or {}).get("context") or {}).get("id")
    if not context_wamid:
        return None

    ctx_msg = db.query(Message).filter(
        Message.wamid == context_wamid,
        Message.direction == "outbound",
    ).first()
    if not ctx_msg:
        return None
    return (ctx_msg.content or {}).get("template_name")


def _decided(service) -> str | None:
    """The decision already recorded for this order, if any."""
    data = service.data or {}
    if data.get(_CONFIRMED_AT_KEY):
        return "confirmed"
    if data.get(_CANCELLED_AT_KEY):
        return "cancelled"
    return None


def _stamp(service, key: str) -> str:
    """Record a marker on Service.data and return the timestamp written."""
    now = datetime.now(timezone.utc).isoformat()
    data = dict(service.data or {})
    data[key] = now
    service.data = data
    flag_modified(service, "data")   # JSONB mutations are invisible to SQLAlchemy otherwise
    return now


def handle_tap(
    db: Session,
    service,
    account,
    mobile_no: str,
    button_payload: str,
    inbound_msg: Message,
) -> None:
    """Dispatch a template button tap. Never raises."""
    # Payments have no buttons and nothing to approve. The early return matters: a
    # "confirm_order" tap reaching _send_confirmation_step below would send the *order*
    # confirmation template — with Confirm and Cancel — and open a route into
    # approve.py for an order this service never represented.
    if not _is_order(service):
        _unhandled(service, button_payload)
        return

    action = _normalise(button_payload)

    # First decision wins. Once confirmed or cancelled, both buttons on both templates
    # are inert — SFA must never receive an approval followed by a contradiction.
    decision = _decided(service)
    if decision:
        logger.info(
            "Liso: order already %s service=%s — ignoring %r",
            decision, service.service_id, button_payload,
        )
        return

    on_confirm_template = _tapped_template(db, inbound_msg) == settings.LIZO_CONFIRM_TEMPLATE

    if not on_confirm_template:
        # The order message — or a tap we could not attribute, which is treated the
        # same because doing so is idempotent and cannot approve anything.
        if action == _CONFIRM_ORDER:
            _send_confirmation_step(db, service, account, mobile_no)
        else:
            _unhandled(service, button_payload)
        return

    if action == _CONFIRM_ORDER:
        _confirm(db, service, account, mobile_no)
    elif action == _CANCEL:
        _cancel(db, service, account, mobile_no)
    else:
        _unhandled(service, button_payload)


def _unhandled(service, button_payload: str) -> None:
    """
    Record and ignore. Covers "View Order" taps on messages sent before that button
    was dropped, and any button a template gains later.
    """
    logger.info(
        "Liso: unhandled button payload=%r service=%s", button_payload, service.service_id
    )


def _send_confirmation_step(db: Session, service, account, mobile_no: str) -> None:
    """
    Step one: ask the customer to confirm, on a template that can also be cancelled.

    Nothing is reported to the client here — no approval, no status callback. The
    order is not decided until the second template is answered.
    """
    if (service.data or {}).get(_CONFIRM_SENT_KEY):
        logger.info(
            "Liso: confirmation step already sent service=%s — ignoring re-tap",
            service.service_id,
        )
        return

    if not confirm.send_confirm_template(db, service, account, mobile_no):
        # Marker deliberately not stamped: the customer can tap again and retry rather
        # than being left with an order that can never be confirmed.
        return

    _stamp(service, _CONFIRM_SENT_KEY)
    logger.info("Liso: confirmation step sent service=%s", service.service_id)


def _confirm(db: Session, service, account, mobile_no: str) -> None:
    """Step two, Confirm: approve the order and tell everyone."""
    _stamp(service, _CONFIRMED_AT_KEY)
    logger.info("Liso: order confirmed service=%s", service.service_id)

    _post_confirmation(db, service)
    confirm.send_acknowledgement(db, service, account, mobile_no, confirm.ACK_CONFIRMED)


def _cancel(db: Session, service, account, mobile_no: str) -> None:
    """
    Step two, Cancel: acknowledge and stop.

    Nothing is sent to SFA — no ApproveOrder, and no status callback. The order simply
    stays unapproved on their side, which is what an unconfirmed order already looks
    like to them.
    """
    _stamp(service, _CANCELLED_AT_KEY)
    logger.info("Liso: order cancelled by customer service=%s", service.service_id)

    confirm.send_acknowledgement(
        db, service, account, mobile_no, confirm.cancelled_text(service)
    )


def _post_confirmation(db: Session, service) -> None:
    """
    Hand the confirmation to the client — two separate calls to two SFA endpoints.

      approve.emit   POST /api/Sfa/ApproveOrder            — approves the order
      notify.emit    POST /api/Sfa/SaveWhatsAppOrderStatus — reports "Confirmed"

    The approval goes first: it is the action, the status is the report of it.
    Neither blocks the webhook — both write OutboundNotification rows that
    notify_scheduler delivers, with retries, moments later.

    Status goes through app/lizo/notify.py rather than notify_queue: a Liso service is
    already "completed" by the time a button can be tapped, and notify_queue drops
    non-terminal events on a terminal service. The row it writes still rides
    notify_scheduler's retry and backoff.
    """
    approve.emit(db, service)

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
