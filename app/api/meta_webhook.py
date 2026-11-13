"""
POST /webhook/meta — inbound events from the Meta WhatsApp Business Platform.

Handles:
  - GET  /webhook/meta  → Meta subscription verification handshake
  - POST /webhook/meta  → inbound messages, status receipts, button taps

Auth: HMAC-SHA256 signature on X-Hub-Signature-256, checked against META_APP_SECRET
and any extra secrets in META_APP_SECRETS (one per Meta app subscribed to our WABAs).
Always returns HTTP 200 to Meta once the signature is good (Meta retries on non-200).

Processing depends on WEBHOOK_INBOX_MODE (see app/core/config.py):
  off    — processed in a BackgroundTask with its own session; nothing stored first.
  shadow — stored in webhook_inbox (status 'shadow') AND processed the old way.
  on     — stored in webhook_inbox and committed BEFORE the 200, then processed from
           there; the scheduler sweeps anything the BackgroundTask did not finish.
"""
import hashlib
import hmac
import logging
import traceback as tb_module
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.failed_webhook import FailedWebhook
from app.models.whatsapp import WhatsAppAccount
from app.services import account_health, conversation_engine, webhook_inbox
from app.utils.error_logger import log_error

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["Meta Webhook"])


# ── Verification handshake ────────────────────────────────────────────────────

@router.get("/meta")
def meta_verify(request: Request):
    """Meta calls this once when registering the webhook subscription."""
    params    = request.query_params
    mode      = params.get("hub.mode")
    token     = params.get("hub.verify_token")
    challenge = params.get("hub.challenge", "")

    if mode == "subscribe" and token == settings.META_WEBHOOK_VERIFY_TOKEN:
        return PlainTextResponse(challenge)
    return PlainTextResponse("Forbidden", status_code=403)


# ── Inbound event handler ─────────────────────────────────────────────────────

@router.post("/meta")
async def meta_inbound(request: Request, background_tasks: BackgroundTasks):
    """Receive Meta webhook events, verify HMAC, and hand off to background processing."""
    raw_body = await request.body()

    sig_header = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_signature(raw_body, sig_header):
        logger.warning(
            "Meta webhook: invalid signature from %s",
            request.client.host if request.client else "?",
        )
        return JSONResponse({"error": "invalid signature"}, status_code=403)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "ok"})

    mode = (settings.WEBHOOK_INBOX_MODE or "off").lower()
    if mode in ("on", "shadow"):
        db = SessionLocal()
        try:
            row_id = webhook_inbox.store(
                db, raw_body, body, status="pending" if mode == "on" else "shadow")
        except Exception as exc:
            db.rollback()
            log_error("Webhook inbox store failed", "POST /webhook/meta", exc)
            # Not acknowledged: Meta retries, and the retry gets another chance to store.
            # Answering 200 here would lose the event, which is what the inbox prevents.
            if mode == "on":
                return JSONResponse({"error": "temporarily unavailable"}, status_code=503)
            row_id = None
        finally:
            db.close()

        if mode == "on":
            if row_id is not None:
                background_tasks.add_task(webhook_inbox.process_now, row_id)
            # row_id None = an identical body is already stored; nothing more to do.
            return JSONResponse({"status": "ok"})

    background_tasks.add_task(_process_payload_bg, body)

    # Always return 200 immediately — Meta retries on non-200
    return JSONResponse({"status": "ok"})


# ── Signature verification ────────────────────────────────────────────────────

def _app_secrets() -> list[str]:
    """Every secret a genuine delivery may be signed with. Whitespace is stripped: a
    secret pasted into .env with a trailing space or newline is the most common reason
    a correct secret still fails."""
    raw = [settings.META_APP_SECRET or ""] + (settings.META_APP_SECRETS or "").split(",")
    seen: list[str] = []
    for s in raw:
        s = s.strip()
        if s and s not in seen:
            seen.append(s)
    return seen


def _verify_signature(body: bytes, header: str) -> bool:
    secrets = _app_secrets()
    if not secrets:
        logger.warning("META_APP_SECRET not set — skipping signature check (dev mode)")
        return True
    ok = False
    for secret in secrets:
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        # No early exit: every secret is compared, so timing does not reveal which
        # position in the list matched.
        ok = hmac.compare_digest(expected, header or "") or ok
    return ok


# ── Background processor (own DB session) ─────────────────────────────────────

def _process_payload_bg(body: dict) -> None:
    """Run in BackgroundTask — opens and closes its own DB session."""
    db = SessionLocal()
    try:
        _process_payload(body, db)
    except Exception as exc:
        log_error("Meta webhook background processing error", "POST /webhook/meta", exc)
        _store_failed_webhook(body, exc)
    finally:
        db.close()


def _store_failed_webhook(body: dict, exc: Exception) -> None:
    """Persist a failed webhook payload so it can be inspected and replayed."""
    db = SessionLocal()
    try:
        db.add(FailedWebhook(
            source      = "meta",
            raw_payload = body,
            error_type  = type(exc).__name__,
            traceback   = tb_module.format_exc(),
            replayed    = False,
        ))
        db.commit()
    except Exception as store_exc:
        logger.error("Could not store failed webhook: %s", store_exc)
    finally:
        db.close()


def _process_payload(body: dict, db) -> None:
    logger.info("META WEBHOOK payload: %s", body)
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") != "messages":
                # Template status, phone-number quality: account health, not messages.
                if not account_health.handle_field(db, change.get("field"),
                                                   change.get("value") or {},
                                                   entry.get("id")):
                    logger.info("META WEBHOOK skipping field=%s", change.get("field"))
                continue
            value = change.get("value", {})
            meta  = value.get("metadata", {})
            phone_number_id = meta.get("phone_number_id")

            statuses = value.get("statuses", [])
            messages = value.get("messages", [])
            logger.info(
                "META WEBHOOK phone_number_id=%s statuses=%d messages=%d",
                phone_number_id, len(statuses), len(messages),
            )
            if messages:
                logger.info("META WEBHOOK inbound msg type=%s from=%s",
                            messages[0].get("type"), messages[0].get("from"))

            account = None
            if phone_number_id:
                account = db.query(WhatsAppAccount).filter(
                    WhatsAppAccount.phone_number_id == phone_number_id
                ).first()

            if not account:
                logger.info("Meta webhook: no account for phone_number_id=%s", phone_number_id)
                continue

            # For the silence alarm. Its own statement, so a failure in a handler
            # below cannot roll it back — hearing from Meta is true either way.
            _touch_last_webhook(db, account)

            # Status receipts (delivered / read / failed)
            for status in statuses:
                _handle_status(db, status, account)

            # Inbound messages
            for msg in messages:
                _handle_message(db, msg, account)


def _touch_last_webhook(db, account) -> None:
    try:
        account.last_webhook_at = datetime.now(timezone.utc)
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("Could not record last_webhook_at: %s", exc)


def _handle_status(db, status: dict, account) -> None:
    conversation_engine.handle_status(db, status, account)


def _handle_message(db, msg: dict, account) -> None:
    conversation_engine.handle_inbound(db, account, msg)
    # Only reached when handling succeeded, so the fast-path key never marks a
    # message that was not actually stored.
    conversation_engine.remember_wamid(msg.get("id"))
