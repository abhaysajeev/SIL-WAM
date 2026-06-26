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
SendResult = namedtuple("SendResult", ["ok", "meta_message_id", "error"])


def send_template(
    account: WhatsAppAccount,
    template: WhatsAppTemplate,
    body_params: list[str],
    to_phone: str,
    cta_urls: dict[str, str] | None = None,
) -> SendResult:
    """
    Send a WhatsApp template message via the Meta Graph API.

    body_params  — ordered list of substitution values for {{1}}, {{2}}, ...
    cta_urls     — {button_index_str: url_value} for URL-button components, or None
    """
    components = []

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
        return SendResult(ok=False, meta_message_id=None, error=err_msg)
    except Exception as e:
        log_error(
            "WhatsApp template send failed",
            f"wa_sender.send_template → {account.phone_number_id}",
            e,
        )
        return SendResult(ok=False, meta_message_id=None, error="WhatsApp send failed (internal error)")


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
    return _post_message(account, message_body, "send_interactive_buttons")


def send_list_message(
    account: WhatsAppAccount,
    to_phone: str,
    body: str,
    button_label: str,
    sections: list[dict],
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
        return SendResult(ok=False, meta_message_id=None, error=err_msg)
    except Exception as e:
        log_error(
            f"WhatsApp {caller} failed",
            f"wa_sender.{caller} → {account.phone_number_id}",
            e,
        )
        return SendResult(ok=False, meta_message_id=None, error="WhatsApp send failed (internal error)")
