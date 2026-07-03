"""
app/services/notify_queue.py — enqueue outbound status notifications for clients.

Called from every place a Service or one of its Messages changes state that a
client cares about:
  - conversation_engine.handle_status()     → message-level: sent | delivered | read
  - conversation_engine.handle_inbound()    → service-level: responded (first CTA tap)
  - conversation_engine._complete_service() → service-level: completed
  - expiry_scheduler / queue_manager        → service-level: expired | failed

Each call fully materializes the notification payload (including whatever
ServiceResponse rows exist at that moment) into OutboundNotification.payload —
notify_scheduler.py just POSTs the stored JSON later, no re-computation at send time.

Only models are imported here (no service-layer imports) so this module can be
safely imported from conversation_engine, queue_manager, and expiry_scheduler
without circular-import risk.
"""
from sqlalchemy.orm import Session

from app.models.api_key import CompanyApiKey
from app.models.conversation import Message, Service, ServiceResponse
from app.models.outbound_notification import OutboundNotification


def enqueue_notification(
    db: Session,
    service: Service,
    event_status: str,   # "sent" | "delivered" | "read" | "responded" | "completed" | "expired" | "failed"
    *,
    message: Message | None = None,
    note: str = "",
) -> None:
    """
    No-op if the service wasn't created via client-api (no api_key_id) or its
    key has no notify_url configured — most Services (demo, ERPNext) fall here.

    note is accepted but not yet surfaced in the payload (message stays null) —
    client hasn't finalized what should populate it (e.g. stray/off-flow replies).
    """
    if not service.api_key_id:
        return

    api_key = db.query(CompanyApiKey).filter(CompanyApiKey.id == service.api_key_id).first()
    if not api_key or not api_key.notify_url:
        return

    payload = _build_payload(db, service, event_status, message)

    db.add(OutboundNotification(
        service_id = service.id,
        message_id = message.id if message else None,
        notify_url = api_key.notify_url,
        payload    = payload,
    ))


def _build_payload(
    db: Session,
    service: Service,
    event_status: str,
    message: Message | None,
) -> dict:
    # The app's session has autoflush=False (app/core/database.py), so when this
    # runs in the same transaction as the ServiceResponse that just triggered a
    # "completed" event (conversation_engine._complete_service), that row hasn't
    # been sent to the DB yet and this query would miss it. Flush first so pending
    # writes in this transaction are visible to the SELECT below.
    db.flush()

    responses = (
        db.query(ServiceResponse)
        .filter(ServiceResponse.service_id == service.id)
        .order_by(ServiceResponse.sequence)
        .all()
    )

    # respondedOn is fixed to the customer's first-ever inbound interaction with this
    # service (tapping the template's CTA button counts, same as answering a question)
    # — same value repeated on every subsequent notification, not "when this event
    # happened". Null until the customer engages at all.
    first_inbound = (
        db.query(Message.created_at)
        .filter(
            Message.service_id      == service.id,
            Message.direction       == "inbound",
            Message.is_flow_message.is_(True),
        )
        .order_by(Message.created_at.asc())
        .first()
    )
    responded_on = first_inbound[0].isoformat() if first_inbound else None

    return {
        "service_id":   service.service_id,
        "reference_id": str(service.id),
        "status":       event_status,
        "respondedOn":  responded_on,
        "message":      None,
        # Which specific message (template / a given question) this event is about.
        # Null for service-level events (completed/expired/failed).
        "context":      message.content if message else None,
        "responses": [
            {
                "field_key":   r.field_key,
                # Translate back to the client's 1-indexed convention (mirrors the
                # -1 applied on ingest in client_services_api.py::ingest_service).
                "answer_type": r.answer_type + 1,
                "response":    r.response_value,
            }
            for r in responses
        ],
    }
