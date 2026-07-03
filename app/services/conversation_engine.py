"""
app/services/conversation_engine.py — Inbound message router and conversation state machine.

Called from meta_webhook.py BackgroundTasks (with its own DB session).

Flow per inbound message:
  1. Dedup by wamid (DB unique index — silently skip if already stored)
  2. Get/create Conversation for (company_id, mobile_no)
  3. Resolve which Service this message belongs to — concurrency is unlimited, so
     several services can be active for the same mobile at once. Interactive/button
     replies carry Meta's context.id (the wamid replied to), resolved deterministically
     via _resolve_service_from_context. Plain text has no such reference, so it falls
     back to whichever ONE service (if exactly one) has an outstanding free-text
     question — see _has_outstanding_free_text.
  4. Store inbound Message
  5. If no active queue → random out-of-flow message, done
  6. Route based on whether it's a template button click or a Q&A reply
  7. Validate response type matches expected answer_type
  8. Record ServiceResponse, advance or complete the flow
"""
import logging
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.core.redis_client import get_redis
from app.models.conversation import (
    Conversation,
    Message,
    MobileQueue,
    Service,
    ServiceResponse,
)
from app.models.erpnext_config import ERPNextConfig
from app.models.whatsapp import WhatsAppAccount
from app.services import notify_queue, queue_manager, wa_sender
from app.utils.error_logger import log_error

_WAMID_TTL = 86_400  # 24 h — wamid cache expiry in Redis

logger = logging.getLogger(__name__)


# ── Public entry points ───────────────────────────────────────────────────────

def handle_inbound(db: Session, account: WhatsAppAccount, msg: dict) -> None:
    """Route an inbound customer message through the conversation/service flow."""
    wamid    = msg.get("id")
    from_no  = msg.get("from", "")
    msg_type = msg.get("type", "")

    # 1. DEDUP — Redis fast path, then DB unique index as authoritative fallback
    if wamid:
        redis = get_redis()
        if redis is not None:
            try:
                key = f"wamid:{wamid}"
                if redis.get(key):
                    logger.debug("Duplicate wamid=%s (Redis) — skipping", wamid)
                    return
                redis.setex(key, _WAMID_TTL, "1")
            except Exception as redis_exc:
                logger.warning("Redis dedup error — falling through to DB: %s", redis_exc)
        if db.query(Message).filter(Message.wamid == wamid).first():
            logger.debug("Duplicate wamid=%s (DB) — skipping", wamid)
            return

    # 2. GET/CREATE CONVERSATION
    # Normalise from_no: Meta always sends the full international number (e.g. 917025985366)
    # but stored numbers may have been ingested without a country code (e.g. 7025985366).
    # Resolve to the canonical stored form so all lookups hit the right rows.
    from_no = _resolve_stored_mobile(db, account.company_id, from_no)
    conv = _get_or_create_conversation(db, account.company_id, from_no)

    # 3. RESOLVE WHICH SERVICE THIS MESSAGE BELONGS TO — see module docstring.
    context_wamid = (msg.get("context") or {}).get("id")
    service = None

    if msg_type in ("interactive", "button"):
        service = _resolve_service_from_context(db, context_wamid)
    elif msg_type == "text":
        service = _resolve_service_from_context(db, context_wamid)  # rare, but honor if Meta attaches it
        if service is None:
            candidates = (
                db.query(Service)
                .filter(Service.conversation_id == conv.id, Service.status == "in_progress")
                .all()
            )
            awaiting = [
                s for s in candidates
                if any(
                    q.get("answer_type") == 2 and q.get("dispatched") == 1 and q.get("sent") == 0
                    for q in (s.questions or [])
                )
            ]
            if len(awaiting) == 1:
                service = awaiting[0]
            # 0 or >1 awaiting → ambiguous/none → stays out-of-flow, matching prior behavior

    # No status filter here — a duplicate/retried webhook for an already-completed
    # service must still find this row (its status is now "completed", not
    # "in_progress") so the idempotent-complete path below doesn't crash on None.
    queue_entry = None
    if service:
        queue_entry = db.query(MobileQueue).filter(
            MobileQueue.service_id == service.id,
        ).first()

    # 4. STORE INBOUND MESSAGE
    inbound = Message(
        conversation_id = conv.id,
        service_id      = service.id if service else None,
        wamid           = wamid,
        direction       = "inbound",
        message_type    = msg_type,
        content         = msg,
        is_flow_message = service is not None,
    )
    db.add(inbound)

    # 5. UPDATE CONVERSATION STATS
    conv.last_activity_at = datetime.now(timezone.utc)
    conv.total_messages   = (conv.total_messages or 0) + 1

    # 6. NO ACTIVE SERVICE — could be a PDF button tap or a random out-of-flow message
    if not service:
        if msg_type == "button":
            button_payload = (msg.get("button") or {}).get("payload", "")
            if _is_download_invoice(button_payload):
                _handle_pdf_request(db, account, from_no, conv, msg)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()  # duplicate wamid race condition — safe to ignore
        return

    # 7. TEMPLATE BUTTON CLICK (customer tapped the CTA on the template message)
    if msg_type == "button":
        button_payload = (msg.get("button") or {}).get("payload", "")
        if _is_download_invoice(button_payload):
            # PDF request during an active service flow — handle it and leave flow intact
            _handle_pdf_request(db, account, from_no, conv, msg)
            db.commit()
            return
        # First-ever engagement → notify once. Buttons stay clickable forever, so a
        # re-tap must not re-fire this — question["sent"] only flips on an ANSWER
        # (step 12 below), so it can't detect "already fired but not yet answered".
        # Check for a prior non-template outbound flow message instead — only
        # _fire_next_question creates those before this service reaches completion.
        already_engaged = db.query(Message).filter(
            Message.service_id      == service.id,
            Message.direction       == "outbound",
            Message.is_flow_message.is_(True),
            Message.message_type    != "template",
        ).first() is not None
        if not already_engaged:
            notify_queue.enqueue_notification(db, service, "responded", message=inbound)
        _fire_next_question(db, service, account, from_no)
        db.commit()
        return

    # 8. EXTRACT RESPONSE VALUE based on message type
    response_value = _extract_response_value(msg, msg_type)

    # 9. FIND NEXT UNANSWERED QUESTION
    questions = list(service.questions or [])
    next_q = next((q for q in questions if q.get("sent") == 0), None)

    if next_q is None:
        # All questions already answered — idempotent complete
        _complete_service(db, service, queue_entry, account)
        db.commit()
        return

    # 10. STALE BUTTON/LIST GUARD — WhatsApp buttons stay clickable forever.
    # Meta includes context.id (the wamid of the outbound question message the user
    # replied to) on every interactive reply.  If that wamid belongs to an already-
    # answered question (sequence < next_q sequence), discard the tap silently.
    if msg_type == "interactive":
        context_wamid = (msg.get("context") or {}).get("id")
        if context_wamid:
            ctx_msg = db.query(Message).filter(
                Message.wamid      == context_wamid,
                Message.direction  == "outbound",
                Message.is_flow_message == True,
            ).first()
            if ctx_msg:
                stored_seq = (ctx_msg.content or {}).get("sequence")
                if stored_seq is not None and stored_seq != next_q["sequence"]:
                    logger.info(
                        "Stale interactive tap: replied to seq=%s but current unanswered is seq=%s — resending current question",
                        stored_seq, next_q["sequence"],
                    )
                    inbound.is_flow_message = False
                    inbound.service_id      = None
                    _fire_next_question(db, service, account, from_no)
                    db.commit()
                    return

    # 11. VALIDATE RESPONSE TYPE
    if not _is_valid_response(msg_type, next_q.get("answer_type")):
        # Unexpected response type — reclassify as random, re-send current question
        inbound.is_flow_message = False
        inbound.service_id      = None
        _resend_question(db, service, account, from_no, next_q)
        db.commit()
        return

    # 11. RECORD RESPONSE
    db.add(ServiceResponse(
        service_id     = service.id,
        sequence       = next_q["sequence"],
        field_key      = next_q["field_key"],
        question       = next_q["question"],
        answer_type    = next_q["answer_type"],
        response_value = response_value,
    ))

    # 12. MARK QUESTION AS ANSWERED
    next_q["sent"] = 1
    service.questions = questions
    flag_modified(service, "questions")  # force SQLAlchemy to detect JSONB mutation

    # 13. ADVANCE OR COMPLETE
    remaining = [q for q in questions if q.get("sent") == 0]
    if remaining:
        _fire_next_question(db, service, account, from_no)
    else:
        _complete_service(db, service, queue_entry, account)

    db.commit()


def handle_status(db: Session, status: dict, account: WhatsAppAccount) -> None:
    """Update message delivery/read status from a Meta status receipt."""
    wamid     = status.get("id")
    state     = status.get("status")   # sent | delivered | read | failed
    timestamp = status.get("timestamp")

    if not wamid or not state:
        return

    msg = db.query(Message).filter(Message.wamid == wamid).first()
    if not msg:
        logger.debug("Status receipt for unknown wamid=%s state=%s", wamid, state)
        return

    msg.status = state
    try:
        ts = datetime.fromtimestamp(int(timestamp), tz=timezone.utc) if timestamp else None
    except (TypeError, ValueError):
        ts = None

    if state == "delivered" and ts:
        msg.delivered_at = ts
    elif state == "read" and ts:
        msg.read_at = ts

    if msg.service_id:
        service = db.query(Service).filter(Service.id == msg.service_id).first()
        if service:
            # notify_queue.enqueue_notification enforces monotonic status progression
            # (sent < delivered < read < responded < completed) — a question's own
            # delivery receipts arriving after "responded"/"completed" already fired
            # are automatically suppressed there, not re-checked here.
            notify_queue.enqueue_notification(db, service, state, message=msg)

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        log_error("Status receipt update failed", f"handle_status wamid={wamid}", exc)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _resolve_stored_mobile(db: Session, company_id, from_no: str) -> str:
    """Return the canonical mobile_no stored in DB for this inbound number.

    Meta sends full international numbers (e.g. 917025985366) but clients
    often ingest without a country code (e.g. 7025985366).  Try exact match
    first; if nothing found, try matching any stored number that is a suffix
    of from_no so the conversation/queue lookups always hit the right rows.
    """
    exact = db.query(Conversation.mobile_no).filter(
        Conversation.company_id == company_id,
        Conversation.mobile_no  == from_no,
    ).first()
    if exact:
        return from_no

    if len(from_no) > 7:
        for trim in range(1, len(from_no) - 6):
            candidate = from_no[trim:]
            match = db.query(Conversation.mobile_no).filter(
                Conversation.company_id == company_id,
                Conversation.mobile_no  == candidate,
            ).first()
            if match:
                logger.debug(
                    "Mobile normalised: %s → %s (stripped %d leading digits)",
                    from_no, candidate, trim,
                )
                return candidate

    return from_no


def _get_or_create_conversation(
    db: Session, company_id, mobile_no: str
) -> Conversation:
    conv = db.query(Conversation).filter(
        Conversation.company_id == company_id,
        Conversation.mobile_no  == mobile_no,
    ).first()
    if not conv:
        conv = Conversation(company_id=company_id, mobile_no=mobile_no)
        db.add(conv)
        db.flush()
    return conv


def _resolve_service_from_context(db: Session, context_wamid: str | None) -> Service | None:
    """Resolve the Service a reply belongs to via Meta's context.id (the wamid of
    the outbound message being replied to) — the one deterministic, concurrency-safe
    routing signal Meta provides. Works for template-CTA taps and interactive replies."""
    if not context_wamid:
        return None
    ctx_msg = db.query(Message).filter(
        Message.wamid == context_wamid, Message.direction == "outbound"
    ).first()
    if ctx_msg and ctx_msg.service_id:
        return db.query(Service).filter(Service.id == ctx_msg.service_id).first()
    return None


def _has_outstanding_free_text(db: Session, conversation_id, exclude_service_id=None) -> bool:
    """True if some active service in this conversation currently has a
    dispatched-but-unanswered free-text question. Only one may exist at a time per
    mobile number, since Meta gives no reply-threading for plain text messages."""
    # The app's session has autoflush=False (app/core/database.py), so a status
    # change made moments earlier in the same transaction (e.g. this service's own
    # answer being recorded, or another service just completing) isn't visible to
    # this query until flushed.
    db.flush()
    candidates = db.query(Service).filter(
        Service.conversation_id == conversation_id, Service.status == "in_progress"
    )
    if exclude_service_id is not None:
        candidates = candidates.filter(Service.id != exclude_service_id)
    for svc in candidates.all():
        for q in (svc.questions or []):
            if q.get("answer_type") == 2 and q.get("dispatched") == 1 and q.get("sent") == 0:
                return True
    return False


def _release_free_text_slot(db: Session, account: WhatsAppAccount, conversation_id) -> None:
    """Called whenever a service in this conversation reaches a terminal state
    (completed/expired/failed). If another in_progress service in the same
    conversation has a held free-text question next in line, fire it now."""
    db.flush()  # see _has_outstanding_free_text — same autoflush=False caveat
    candidates = (
        db.query(Service)
        .filter(Service.conversation_id == conversation_id, Service.status == "in_progress")
        .order_by(Service.created_at)
        .all()
    )
    for svc in candidates:
        next_q = next((q for q in (svc.questions or []) if q.get("sent") == 0), None)
        if next_q and next_q.get("answer_type") == 2 and next_q.get("dispatched") == 0:
            mobile_no = (svc.data or {}).get("customer_mobile")
            if mobile_no:
                _fire_next_question(db, svc, account, mobile_no)
            break


def _is_valid_response(msg_type: str, answer_type: int | None) -> bool:
    """True if the Meta message type matches the expected answer type."""
    if answer_type in (0, 1):       # yes/no buttons or rating
        return msg_type == "interactive"
    if answer_type == 2:            # free text
        return msg_type == "text"
    return False


def _extract_response_value(msg: dict, msg_type: str) -> str | None:
    """Pull the human-readable response value out of a Meta message dict."""
    if msg_type == "interactive":
        interactive = msg.get("interactive", {})
        itype = interactive.get("type")
        if itype == "button_reply":
            return interactive.get("button_reply", {}).get("title")
        if itype == "list_reply":
            return interactive.get("list_reply", {}).get("title")
    if msg_type == "text":
        body = msg.get("text", {}).get("body", "")
        return body[:4096]
    return None


def _fire_next_question(
    db: Session, service: Service, account: WhatsAppAccount, mobile_no: str
) -> None:
    """Send the next unanswered question to the customer.

    Every button/list question shows the order number as a native Meta footer.
    Free-text questions have no footer slot in Meta's API, so the order number is
    appended into the body text instead. Free-text questions are additionally
    serialized across concurrently active services for the same mobile — see
    _has_outstanding_free_text — since Meta gives no reply-threading for plain text.
    """
    questions = service.questions or []
    next_q = next((q for q in questions if q.get("sent") == 0), None)
    if next_q is None:
        return

    answer_type = next_q.get("answer_type")
    question_text = next_q.get("question", "")

    if answer_type == 2 and _has_outstanding_free_text(
        db, service.conversation_id, exclude_service_id=service.id
    ):
        logger.debug(
            "Holding free-text question seq=%s for service=%s — another free-text "
            "question is outstanding for this mobile",
            next_q.get("sequence"), service.service_id,
        )
        return

    result = None
    msg_type_out = "text"
    footer = service.service_id

    if answer_type == 0:
        # Yes/No or custom buttons
        options = next_q.get("options") or ["Yes", "No"]
        buttons = [
            {"id": f"q{next_q['sequence']}_opt{i}", "title": str(opt)[:20]}
            for i, opt in enumerate(options[:3])  # Meta max 3 buttons
        ]
        result = wa_sender.send_interactive_buttons(account, mobile_no, question_text, buttons, footer=footer)
        msg_type_out = "interactive"

    elif answer_type == 1:
        # Rating
        scale = next_q.get("rating_scale", 5)
        if scale <= 3:
            buttons = [
                {"id": f"q{next_q['sequence']}_r{i+1}", "title": str(i + 1)}
                for i in range(scale)
            ]
            result = wa_sender.send_interactive_buttons(account, mobile_no, question_text, buttons, footer=footer)
            msg_type_out = "interactive"
        else:
            rows = [
                {"id": f"q{next_q['sequence']}_r{i+1}", "title": str(i + 1)}
                for i in range(scale)
            ]
            sections = [{"title": "Rating", "rows": rows}]
            result = wa_sender.send_list_message(
                account, mobile_no, question_text, "Rate", sections, footer=footer
            )
            msg_type_out = "interactive"

    elif answer_type == 2:
        # Free text — no native footer slot, append order number into the body
        result = wa_sender.send_text(account, mobile_no, f"{question_text}\n\n_Order: {service.service_id}_")
        msg_type_out = "text"

    if result and result.ok:
        next_q["dispatched"] = 1
        service.questions = questions
        flag_modified(service, "questions")
        db.add(Message(
            conversation_id = service.conversation_id,
            service_id      = service.id,
            wamid           = result.meta_message_id,
            direction       = "outbound",
            message_type    = msg_type_out,
            content         = {
                "question":    question_text,
                "answer_type": answer_type,
                "sequence":    next_q["sequence"],
                "field_key":   next_q.get("field_key"),
            },
            is_flow_message = True,
            status          = "sent",
            sent_at         = datetime.now(timezone.utc),
        ))
    elif result:
        log_error(
            f"Question send failed (service={service.service_id} seq={next_q.get('sequence')})",
            f"conversation_engine._fire_next_question → {mobile_no}",
            Exception(result.error or "unknown"),
        )


def _resend_question(
    db: Session, service: Service, account: WhatsAppAccount,
    mobile_no: str, question: dict,
) -> None:
    """Re-send the current (unanswered) question without advancing the flow."""
    _fire_next_question(db, service, account, mobile_no)


def _complete_service(
    db: Session, service: Service, queue_entry: MobileQueue | None,
    account: WhatsAppAccount,
) -> None:
    """Mark service completed, send optional completion message, advance queue.

    Idempotent: a duplicate/retried webhook can resolve to an already-completed
    service (see handle_inbound's "idempotent complete" branch) — bail out before
    re-sending the completion message or re-firing the "completed" notification.
    queue_entry can be None in that case (its status is no longer "in_progress").
    """
    if service.status == "completed":
        return

    service.status       = "completed"
    service.completed_at = datetime.now(timezone.utc)
    if queue_entry:
        queue_entry.status = "completed"

    notify_queue.enqueue_notification(db, service, "completed")

    mobile_no = (service.data or {}).get("customer_mobile", "")

    # Send completion_message if client included it in data
    completion_msg = (service.data or {}).get("completion_message")
    if completion_msg and mobile_no:
        # No native footer slot on a text message — append the order number to the body.
        result = wa_sender.send_text(account, mobile_no, f"{completion_msg}\n\n_Order: {service.service_id}_")
        if result.ok:
            db.add(Message(
                conversation_id = service.conversation_id,
                service_id      = service.id,
                wamid           = result.meta_message_id,
                direction       = "outbound",
                message_type    = "text",
                content         = {"body": completion_msg},
                is_flow_message = True,
                status          = "sent",
                sent_at         = datetime.now(timezone.utc),
            ))

    # Advance queue — start next waiting service for this mobile
    if mobile_no:
        queue_manager.advance_queue(db, mobile_no, service.company_id, account)

    # Another concurrent service on this mobile may have a free-text question
    # held back waiting on this one — release it now that this service is done.
    try:
        _release_free_text_slot(db, account, service.conversation_id)
    except Exception as exc:
        log_error(
            f"_release_free_text_slot failed after completion service={service.service_id}",
            "conversation_engine._complete_service",
            exc,
        )


def _is_download_invoice(payload: str) -> bool:
    """Match the button payload regardless of case or separator style.

    The template has no explicit payload set, so Meta sends the button
    text verbatim: 'Download Invoice'. Normalise before comparing so the
    check doesn't break if the template is ever recreated with an explicit
    payload like 'download_invoice'.
    """
    return payload.lower().replace(" ", "_") == "download_invoice"


def _handle_pdf_request(
    db: Session,
    account: WhatsAppAccount,
    mobile_no: str,
    conv: Conversation,
    msg: dict,
) -> None:
    """
    Customer tapped a 'download_invoice' button — send the invoice PDF via WhatsApp.

    Uses context.id (the wamid of the template message that was tapped) to find the
    exact Service, so tapping an old button never sends the wrong invoice or a duplicate.
    Never raises — logs errors and returns silently so the engine doesn't crash.
    """
    from app.services import erpnext_client  # local import avoids circular dep at module level

    # Use context.id to identify which template message was tapped, then find its Service.
    # Falls back to most-recent service if context is missing (shouldn't happen in practice).
    context_wamid = (msg.get("context") or {}).get("id")
    recent_svc = _resolve_service_from_context(db, context_wamid)

    if not recent_svc:
        recent_svc = (
            db.query(Service)
            .filter(Service.conversation_id == conv.id)
            .order_by(Service.created_at.desc())
            .first()
        )

    if not recent_svc or not recent_svc.data:
        logger.info("PDF request: no service data found for conv=%s", conv.id)
        return

    invoice_no = recent_svc.data.get("invoice_no")
    if not invoice_no:
        logger.info("PDF request: no invoice_no in service data for conv=%s", conv.id)
        return

    # Guard against duplicate sends — once PDF is sent for a service, block re-sends.
    if recent_svc.data.get("pdf_sent"):
        logger.info("PDF request: already sent for service=%s invoice_no=%s — skipping", recent_svc.service_id, invoice_no)
        return

    # Fast path — use pre-cached media_id uploaded at template-send time
    cached_media_id = (recent_svc.data or {}).get("pdf_media_id")

    if cached_media_id:
        logger.info("PDF request: using cached media_id for invoice_no=%s", invoice_no)
        try:
            result = wa_sender.send_document(
                account, mobile_no, cached_media_id,
                filename=f"{invoice_no}.pdf",
                caption=f"Invoice {invoice_no}",
            )
        except Exception as exc:
            log_error(
                f"PDF send (cached) failed for invoice_no={invoice_no}",
                f"conversation_engine._handle_pdf_request → {mobile_no}",
                exc,
            )
            return
    else:
        # Slow path fallback — pre-fetch not ready yet, fetch and upload on demand
        logger.info("PDF request: no cached media_id, fetching on demand for invoice_no=%s", invoice_no)
        config = db.query(ERPNextConfig).filter(
            ERPNextConfig.company_id == account.company_id,
            ERPNextConfig.is_active  == True,
        ).first()
        if not config:
            logger.info("PDF request: no active ERPNext config for company_id=%s", account.company_id)
            return
        try:
            pdf_bytes = erpnext_client.fetch_invoice_pdf(config, invoice_no)
            cached_media_id = erpnext_client.upload_to_meta(account, pdf_bytes, f"{invoice_no}.pdf")
            result = wa_sender.send_document(
                account, mobile_no, cached_media_id,
                filename=f"{invoice_no}.pdf",
                caption=f"Invoice {invoice_no}",
            )
        except Exception as exc:
            log_error(
                f"PDF flow error for invoice_no={invoice_no}",
                f"conversation_engine._handle_pdf_request → {mobile_no}",
                exc,
            )
            return

    if result.ok:
        db.add(Message(
            conversation_id = conv.id,
            service_id      = recent_svc.id,
            wamid           = result.meta_message_id,
            direction       = "outbound",
            message_type    = "document",
            content         = {"invoice_no": invoice_no, "filename": f"{invoice_no}.pdf"},
            is_flow_message = False,
            status          = "sent",
            sent_at         = datetime.now(timezone.utc),
        ))
        # Mark PDF as sent so any future taps on old template buttons are blocked
        data = dict(recent_svc.data)
        data["pdf_sent"] = True
        recent_svc.data = data
        flag_modified(recent_svc, "data")
    else:
        log_error(
            f"PDF document send failed for invoice_no={invoice_no}",
            f"conversation_engine._handle_pdf_request → {mobile_no}",
            Exception(result.error or "unknown send error"),
        )
