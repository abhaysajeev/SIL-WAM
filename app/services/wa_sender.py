"""
app/services/wa_sender.py — all outbound Meta WhatsApp API sends.

Rules for this layer:
- Never raise HTTPException; never import from fastapi.
- Return SendResult — callers decide how to surface errors.
- Sync httpx only; no async.
- Log unexpected exceptions via log_error() before returning a failure result.
"""
import logging
from collections import namedtuple

import httpx

from app.models.whatsapp import WhatsAppAccount, WhatsAppTemplate
from app.utils.error_logger import log_error
from app.utils.whatsapp_crypto import decrypt_token

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.facebook.com/v21.0"

# ok          — True if Meta accepted the message
# meta_message_id — wamid string (e.g. "wamid.xxx") on success, None on failure
# error       — human-readable error string on failure, None on success
# is_transient — True if retrying this exact send could plausibly succeed
#
# is_transient must be decided HERE, at the except/status site, because that is the
# only place the exception type and HTTP status still exist. By the time a caller sees
# the result it has only `error`, a human-readable string, and classifying a timeout by
# matching on that string would be guesswork that silently breaks when a message
# changes. Defaults to False so a failure is terminal unless something positively
# identifies it as retryable.
SendResult = namedtuple(
    "SendResult",
    ["ok", "meta_message_id", "error", "is_transient"],
    defaults=[False],
)


# 5xx is Meta having a bad moment; 429 is us being asked to slow down. Both are worth
# another attempt. Every other 4xx is a statement about the request itself, which will
# be just as wrong the second time.
def _status_is_transient(status_code: int) -> bool:
    return status_code >= 500 or status_code == 429


def _exception_is_transient(exc: Exception) -> bool:
    """
    Network-level failures only.

    httpx.TransportError covers timeouts, connection refusals, DNS failures and read
    errors — the send may never have reached Meta, so retrying is both safe and likely
    to work. Anything else reaching this handler is a bug in our own code, and retrying
    a bug just runs it again.
    """
    return isinstance(exc, httpx.TransportError)


def send_template(
    account: WhatsAppAccount,
    template: WhatsAppTemplate,
    body_params: list[str],
    to_phone: str,
    cta_urls: dict[str, str] | None = None,
    header_media: dict | None = None,
) -> SendResult:
    """
    Send a WhatsApp template message via the Meta Graph API.

    body_params  — ordered list of substitution values for {{1}}, {{2}}, ...
    cta_urls     — {button_index_str: url_value} for URL-button components, or None
    header_media — {"format": "image"|"document"|"video", "filename": ...} plus exactly
                   one of:
                     "link"     — a public URL Meta fetches itself at send time, so it
                                  must be reachable over HTTPS from Meta's servers
                     "media_id" — a file already uploaded to this phone number via
                                  meta_media.upload_media(); Meta serves it from its own
                                  storage, which is what a broadcast wants: one upload
                                  for two thousand sends instead of two thousand fetches
                                  of somebody's web server
                   or None for a text header. `media_id` wins if both are present.

    header_media is deliberately the last parameter: queue_manager and
    broadcast_scheduler both call this positionally, and several tests assert on
    `call_args.args[2]` / `args[3]`. Appending keeps those positions stable.
    """
    components = []

    # Meta expects header before body. Order is not enforced by the API, but a payload
    # that reads in message order is far easier to debug against their error messages.
    if header_media and (header_media.get("media_id") or header_media.get("link")):
        fmt = str(header_media.get("format", "image")).lower()
        media_id = header_media.get("media_id")
        media_obj: dict = {"id": media_id} if media_id else {"link": header_media["link"]}
        if fmt == "document" and header_media.get("filename"):
            # Without this the customer sees the URL's last path segment as the title.
            media_obj["filename"] = header_media["filename"]
        components.append({
            "type": "header",
            "parameters": [{"type": fmt, fmt: media_obj}],
        })

    if body_params:
        components.append({
            "type": "body",
            "parameters": [{"type": "text", "text": v} for v in body_params],
        })

    if cta_urls:
        for btn_index, url_value in cta_urls.items():
            if url_value:
                components.append({
                    "type": "button",
                    "sub_type": "url",
                    "index": str(btn_index),
                    "parameters": [{"type": "text", "text": url_value}],
                })

    message_body = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "template",
        "template": {
            "name": template.name,
            "language": {"code": template.language},
            "components": components,
        },
    }

    try:
        access_token = decrypt_token(account.access_token_encrypted)
        with httpx.Client(timeout=20) as client:
            res = client.post(
                f"{GRAPH_BASE}/{account.phone_number_id}/messages",
                json=message_body,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        data = res.json()
        if res.status_code == 200 and data.get("messages"):
            # Capture the wamid — needed for delivery/read receipt matching
            wamid = data["messages"][0].get("id")
            return SendResult(ok=True, meta_message_id=wamid, error=None)
        logger.error("Meta template send failed HTTP %s: %s | payload: %s", res.status_code, data, message_body)
        err_msg = data.get("error", {}).get("message", f"Meta returned HTTP {res.status_code}")
        return SendResult(ok=False, meta_message_id=None, error=err_msg,
                          is_transient=_status_is_transient(res.status_code))
    except Exception as e:
        log_error(
            "WhatsApp template send failed",
            f"wa_sender.send_template → {account.phone_number_id}",
            e,
        )
        return SendResult(ok=False, meta_message_id=None,
                          error="WhatsApp send failed (internal error)",
                          is_transient=_exception_is_transient(e))


def send_text(
    account: WhatsAppAccount,
    to_phone: str,
    body: str,
) -> SendResult:
    """Send a plain-text WhatsApp message."""
    message_body = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {"body": body},
    }
    return _post_message(account, message_body, "send_text")


def send_interactive_buttons(
    account: WhatsAppAccount,
    to_phone: str,
    body: str,
    buttons: list[dict],  # [{"id": "...", "title": "..."}]  max 3
    footer: str | None = None,
) -> SendResult:
    """Send an interactive reply-button message (max 3 buttons)."""
    message_body = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": b["id"], "title": b["title"]}}
                    for b in buttons
                ]
            },
        },
    }
    if footer:
        message_body["interactive"]["footer"] = {"text": footer[:60]}  # Meta caps footer at 60 chars
    return _post_message(account, message_body, "send_interactive_buttons")


def send_list_message(
    account: WhatsAppAccount,
    to_phone: str,
    body: str,
    button_label: str,
    sections: list[dict],
    footer: str | None = None,
) -> SendResult:
    """Send a list-picker interactive message (supports up to 10 items per section)."""
    message_body = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body},
            "action": {
                "button": button_label,
                "sections": sections,
            },
        },
    }
    if footer:
        message_body["interactive"]["footer"] = {"text": footer[:60]}  # Meta caps footer at 60 chars
    return _post_message(account, message_body, "send_list_message")


def send_document(
    account: WhatsAppAccount,
    to_phone: str,
    media_id: str,
    filename: str,
    caption: str | None = None,
) -> SendResult:
    """Send a document by Meta media ID (already uploaded to Meta's media endpoint)."""
    doc: dict = {"id": media_id, "filename": filename}
    if caption:
        doc["caption"] = caption
    message_body = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "document",
        "document": doc,
    }
    return _post_message(account, message_body, "send_document")


# ── Shared POST helper ────────────────────────────────────────────────────────

def _post_message(account: WhatsAppAccount, message_body: dict, caller: str) -> SendResult:
    """POST to Meta messages endpoint and return a SendResult."""
    try:
        access_token = decrypt_token(account.access_token_encrypted)
        with httpx.Client(timeout=20) as client:
            res = client.post(
                f"{GRAPH_BASE}/{account.phone_number_id}/messages",
                json=message_body,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        data = res.json()
        if res.status_code == 200 and data.get("messages"):
            wamid = data["messages"][0].get("id")
            return SendResult(ok=True, meta_message_id=wamid, error=None)
        err_msg = data.get("error", {}).get("message", f"Meta returned HTTP {res.status_code}")
        # Preserve raw error for 131026 detection by callers
        return SendResult(ok=False, meta_message_id=None, error=err_msg,
                          is_transient=_status_is_transient(res.status_code))
    except Exception as e:
        log_error(
            f"WhatsApp {caller} failed",
            f"wa_sender.{caller} → {account.phone_number_id}",
            e,
        )
        return SendResult(ok=False, meta_message_id=None,
                          error="WhatsApp send failed (internal error)",
                          is_transient=_exception_is_transient(e))
