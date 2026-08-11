"""
Liso's status callback — pushes delivery updates to their SFA endpoint.

Liso's POST returns 201 "in_progress", which only means accepted and queued.
Everything that actually matters happens seconds later: Meta delivers the message,
the customer reads it, the order image turns out to be unfetchable, or the customer
taps Confirm Order. This module is how any of that reaches them.

    POST http://.../api/Sfa/SaveWhatsAppOrderStatus
    {"Credentials": {...}, "RequestData": {ReferenceId, Status, Reason, Timestamp}}

ReferenceId is Service.id — the `reference_id` we returned in the original 201, which
is what Liso stored against their own order.

Why this exists rather than reusing notify_queue
------------------------------------------------
notify_queue.enqueue_notification cannot serve Liso, for two independent reasons:

  1. Its payload is Shirin Asal's envelope. Pointing Liso's notify_url at it would
     POST {"service_id", "status", "reason", "attempt", "responses"} to an endpoint
     expecting Credentials/RequestData.

  2. It suppresses everything we need. A Liso order has no questions, so
     queue_manager completes it the instant the template send returns; its two gates
     (notify_queue.py:88 and :95) then drop every later delivered/read/failed. Liso
     would receive exactly one callback — "completed", fired before Meta had even
     fetched the image — and never hear about a failure.

So this writes OutboundNotification rows directly. notify_scheduler is payload-
agnostic (it POSTs whatever JSONB is in the row to whatever URL is in the row), so
Liso still inherits its 8 retries with backoff to an hour, for free.

Only models are imported here — no service-layer imports — so conversation_engine and
queue_manager can import this without a circular-import risk. Same rule as
notify_queue, and the same reason.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.lizo.schemas import FLOW_MARKER_KEY, FLOW_MARKER_VALUE
from app.models.api_key import CompanyApiKey
from app.models.outbound_notification import OutboundNotification

logger = logging.getLogger(__name__)

# ── The contract ──────────────────────────────────────────────────────────────
# Status is the lifecycle stage, always one of these five words. Reason explains a
# failure in plain English and is empty otherwise. Liso's dashboard shows Status as
# a badge and Reason as the detail, so Status must never carry prose.
STATUS_SENT      = "Sent"
STATUS_DELIVERED = "Delivered"
STATUS_READ      = "Read"
STATUS_CONFIRMED = "Confirmed"
STATUS_FAILED    = "Failed"

ALL_STATUSES = (STATUS_SENT, STATUS_DELIVERED, STATUS_READ, STATUS_CONFIRMED, STATUS_FAILED)

# Meta's own status words → ours. Anything Meta sends that is not in here is ignored
# rather than guessed at.
_META_STATUS = {
    "sent":      STATUS_SENT,
    "delivered": STATUS_DELIVERED,
    "read":      STATUS_READ,
    "failed":    STATUS_FAILED,
}

# Reasons are written for a human reading a dashboard, not copied from Meta. Their
# raw strings name internal concepts ("media upload error") that mean nothing to
# someone looking at an order.
REASON_INVALID_NUMBER = "Invalid WhatsApp number"
REASON_MEDIA          = "Order image could not be downloaded from the supplied URL"
REASON_PARAM_MISMATCH = "Template parameter mismatch"
REASON_GENERIC        = "Message could not be delivered"

# Meta error codes seen on a failed status receipt.
_REASON_BY_META_CODE = {
    131026: REASON_INVALID_NUMBER,   # message undeliverable / not a WhatsApp user
    131052: REASON_MEDIA,            # media download failed
    131053: REASON_MEDIA,            # media upload error
    132000: REASON_PARAM_MISMATCH,   # parameter count mismatch
}

# Our own Service.failed_reason values, for failures that never reached Meta.
_REASON_BY_FAILED_REASON = {
    "whatsapp_number_invalid": REASON_INVALID_NUMBER,
    "media_error":             REASON_MEDIA,
    "send_error":              REASON_GENERIC,
}

# Fixed block SFA expects on every request. Only ServiceName carries meaning to
# them; the rest are their app's device/session fields, irrelevant server-to-server.
# Rebuilt per call rather than shared, so a caller cannot mutate it for everyone.
def _credentials() -> dict:
    return {
        "CheckSum": 0,
        "Operation": 0,
        "Latitude": "",
        "Longitude": "",
        "Altitude": "",
        "DeviceID": "",
        "IMEI": "",
        "LoginUserID": "",
        "ServiceName": "SaveWhatsAppOrderStatus",
        "TokenID": "",
        "BluetoothID": "",
        "IsZipped": 0,
        "CompanyID": 0,
        "SendStatus": 0,
        "ApkType": "",
        "DeviceNotificationID": "",
        "HierarchyTypeID": "",
        "HierarchyID": "",
    }


def handles(service) -> bool:
    """True only for services created through app/lizo/api.py."""
    return (getattr(service, "data", None) or {}).get(FLOW_MARKER_KEY) == FLOW_MARKER_VALUE


def status_for_meta(meta_state: str) -> str | None:
    """Translate Meta's status word, or None if it is one we do not report."""
    return _META_STATUS.get((meta_state or "").lower())


def reason_for(meta_errors: list | None = None, failed_reason: str | None = None) -> str:
    """
    Plain-English cause for a Failed callback.

    Meta's error code is preferred when present — it is the most specific thing we
    know. Falls back to our own failed_reason for sends that never reached Meta at
    all, and finally to Meta's own title so an unmapped code still says something.
    """
    for err in meta_errors or []:
        mapped = _REASON_BY_META_CODE.get(err.get("code"))
        if mapped:
            return mapped

    if failed_reason and failed_reason in _REASON_BY_FAILED_REASON:
        return _REASON_BY_FAILED_REASON[failed_reason]

    for err in meta_errors or []:
        title = (err.get("title") or "").strip()
        if title:
            return title

    return REASON_GENERIC


def emit(
    db: Session,
    service,
    status: str,
    reason: str = "",
    message=None,
    event_at: datetime | None = None,
) -> None:
    """
    Queue one callback. Never raises — a notification problem must not roll back the
    delivery receipt or button tap that triggered it.

    event_at should be Meta's own timestamp from the status receipt where we have it;
    that is when the event actually happened, which can be seconds before we process
    it. Defaults to now for events we originate ourselves, like Confirmed.
    """
    if not handles(service):
        return
    if status not in ALL_STATUSES:
        logger.error("Liso notify: refusing unknown status=%r service=%s", status, service.service_id)
        return
    if not service.api_key_id:
        return

    api_key = db.query(CompanyApiKey).filter(CompanyApiKey.id == service.api_key_id).first()
    if not api_key or not api_key.notify_url:
        # No callback configured — the hooks stay inert rather than erroring.
        return

    db.add(OutboundNotification(
        service_id      = service.id,
        service_attempt = service.attempt_no or 0,
        message_id      = message.id if message else None,
        # Snapshotted, like notify_queue does: the row must still be deliverable if
        # the key's URL changes or the key is deleted before the retries finish.
        notify_url      = api_key.notify_url,
        payload         = _build_payload(service, status, reason, event_at),
    ))
    logger.info(
        "Liso notify queued: service=%s status=%s reason=%r",
        service.service_id, status, reason,
    )


def _build_payload(service, status: str, reason: str, event_at: datetime | None) -> dict:
    return {
        "Credentials": _credentials(),
        "RequestData": {
            # Our reference_id — the UUID returned in the 201 that Liso stored
            # against their order. Not their service_id.
            "ReferenceId": str(service.id),
            "Status":      status,
            "Reason":      reason or "",
            "Timestamp":   _timestamp(event_at),
        },
    }


def _timestamp(dt: datetime | None) -> str:
    """UTC ISO-8601 with milliseconds and a Z suffix — 2026-08-11T05:32:15.761Z."""
    dt = dt or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
