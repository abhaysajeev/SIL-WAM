"""
app/services/erpnext_client.py — ERPNext API calls.

Two responsibilities:
  1. fetch_invoice_pdf  — GET PDF bytes from the customer's ERPNext instance
  2. upload_to_meta     — POST those bytes to Meta's media upload endpoint and return media_id

PDF bytes are never written to disk. The pipeline is:
  ERPNext → base64 → Python bytes → Meta media upload → media_id → send_document()
"""
import base64
import logging

import httpx

from app.core.config import settings
from app.models.erpnext_config import ERPNextConfig
from app.models.whatsapp import WhatsAppAccount
from app.utils.error_logger import log_error
from app.utils.whatsapp_crypto import decrypt_token

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.facebook.com/v22.0"


def fetch_invoice_pdf(config: ERPNextConfig, invoice_no: str) -> bytes:
    """
    Call the ERPNext whitelisted method to get an invoice PDF.

    ERPNext method must return JSON in one of these shapes:
      {"message": {"pdf": "<base64>"}}    ← preferred
      {"message": "<base64>"}             ← fallback (raw base64 string)

    Returns raw PDF bytes. Raises on any error so the caller can log and abort.
    """
    method = config.pdf_method or settings.ERPNEXT_PDF_METHOD
    url    = f"{config.base_url}/api/method/{method}"
    auth   = f"token {config.api_key}:{config.api_secret}"

    try:
        with httpx.Client(timeout=30) as client:
            res = client.get(
                url,
                params={"invoice_no": invoice_no},
                headers={"Authorization": auth},
            )
        res.raise_for_status()
        data = res.json()
        message = data.get("message", "")
        if isinstance(message, dict):
            b64 = message.get("pdf", "")
        else:
            b64 = message
        if not b64:
            raise ValueError(f"ERPNext returned empty PDF payload for invoice_no={invoice_no}")
        return base64.b64decode(b64)
    except Exception as exc:
        log_error(
            f"ERPNext PDF fetch failed for invoice_no={invoice_no}",
            f"erpnext_client.fetch_invoice_pdf → {url}",
            exc,
        )
        raise


def upload_to_meta(
    account: WhatsAppAccount,
    pdf_bytes: bytes,
    filename: str,
) -> str:
    """
    Upload PDF bytes to Meta's media endpoint and return the media_id.

    Meta retains the uploaded media for up to 30 days. The media_id is used
    in send_document() — no disk storage is ever needed.

    Raises on any error so the caller can log and abort.
    """
    access_token = decrypt_token(account.access_token_encrypted)
    url          = f"{GRAPH_BASE}/{account.phone_number_id}/media"

    try:
        with httpx.Client(timeout=60) as client:
            res = client.post(
                url,
                headers={"Authorization": f"Bearer {access_token}"},
                data={"messaging_product": "whatsapp", "type": "application/pdf"},
                files={"file": (filename, pdf_bytes, "application/pdf")},
            )
        data = res.json()
        media_id = data.get("id")
        if not media_id:
            raise ValueError(
                f"Meta media upload returned no id: HTTP {res.status_code} — {data}"
            )
        logger.debug("Meta media upload OK: media_id=%s filename=%s", media_id, filename)
        return media_id
    except Exception as exc:
        log_error(
            f"Meta media upload failed for {filename}",
            f"erpnext_client.upload_to_meta → {account.phone_number_id}",
            exc,
        )
        raise
