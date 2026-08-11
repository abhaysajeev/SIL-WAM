"""
app/services/meta_media.py — uploading media to Meta.

Two uploads that are easy to confuse, because Meta uses the word "upload" for both
and they hit different endpoints, return different opaque strings, and are consumed
in different places:

  upload_resumable() → a **header handle**
      Used only when CREATING or EDITING a message template. Meta needs a sample of
      the media so a human reviewer can see what the header will look like. The handle
      goes into components[HEADER].example.header_handle and is never used again.

  upload_media() → a **media id**
      Used when SENDING a message whose media we hold as bytes. Valid 30 days.
      Not needed for the Lizo flow — that client supplies a public URL and Meta
      fetches it directly — but it is the other half of the pair and belongs here.

`erpnext_client.upload_to_meta` does the same job as upload_media() but with the MIME
type hardcoded to application/pdf. It is deliberately left alone: it sits on the live
ERPNext invoice pipeline, and generalising it in place would put a working production
path at risk to save one small function. Prefer this module for anything new.
"""
import logging

import httpx

from app.core.config import settings
from app.models.whatsapp import WhatsAppAccount
from app.utils.whatsapp_crypto import decrypt_token

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.facebook.com/v22.0"

# What Meta accepts in a template header, per format. Enforced before we spend a
# round trip — Meta's own rejection message for a bad type is not self-explanatory.
# Sizes are Meta's documented caps.
MEDIA_FORMATS: dict[str, dict] = {
    "IMAGE":    {"mimes": ("image/jpeg", "image/png"), "max_bytes": 5  * 1024 * 1024},
    "DOCUMENT": {"mimes": ("application/pdf",),        "max_bytes": 100 * 1024 * 1024},
    "VIDEO":    {"mimes": ("video/mp4",),              "max_bytes": 16 * 1024 * 1024},
}

# Formats that carry media rather than text. Used by callers to decide whether a
# header needs a handle (on create) or a URL (on send).
MEDIA_HEADER_FORMATS = tuple(MEDIA_FORMATS)


class MediaUploadError(Exception):
    """Raised with a message safe to show the user in the template builder."""


def format_for_mime(mime: str) -> str | None:
    """Reverse-lookup the header format a MIME type belongs to, or None if unsupported."""
    for fmt, spec in MEDIA_FORMATS.items():
        if mime in spec["mimes"]:
            return fmt
    return None


def validate_upload(file_bytes: bytes, mime: str) -> str:
    """
    Check a candidate header media file and return its Meta header format.

    Raises MediaUploadError with a human-readable reason — the caller turns it
    straight into a 400 the builder modal can display.
    """
    fmt = format_for_mime(mime)
    if not fmt:
        accepted = ", ".join(sorted(m for s in MEDIA_FORMATS.values() for m in s["mimes"]))
        raise MediaUploadError(f"Unsupported file type '{mime}'. Accepted: {accepted}")

    max_bytes = MEDIA_FORMATS[fmt]["max_bytes"]
    if len(file_bytes) > max_bytes:
        raise MediaUploadError(
            f"{fmt.title()} is {len(file_bytes) / 1048576:.1f} MB — "
            f"Meta's limit is {max_bytes // 1048576} MB."
        )
    if not file_bytes:
        raise MediaUploadError("File is empty.")
    return fmt


def upload_resumable(
    account: WhatsAppAccount,
    file_bytes: bytes,
    filename: str,
    mime: str,
) -> str:
    """
    Upload a template header sample and return Meta's header handle.

    Two steps, both required:
      1. POST /{app_id}/uploads   → an upload session id ("upload:<opaque>")
      2. POST /{session_id}       → the raw bytes, returning {"h": "<handle>"}

    Step 2 authenticates with `Authorization: OAuth <token>` rather than `Bearer` —
    this is not a typo, it is what the Resumable Upload API requires.

    Raises MediaUploadError on any failure.
    """
    if not settings.FB_APP_ID:
        raise MediaUploadError(
            "FB_APP_ID is not configured — template media uploads need the Meta app id."
        )

    validate_upload(file_bytes, mime)
    access_token = decrypt_token(account.access_token_encrypted)

    try:
        with httpx.Client(timeout=120) as client:
            session_res = client.post(
                f"{GRAPH_BASE}/{settings.FB_APP_ID}/uploads",
                params={
                    "file_name": filename,
                    "file_length": len(file_bytes),
                    "file_type": mime,
                },
                headers={"Authorization": f"Bearer {access_token}"},
            )
            session_data = session_res.json()
            session_id = session_data.get("id")
            if not session_id:
                raise MediaUploadError(_meta_message(session_data, session_res.status_code))

            upload_res = client.post(
                f"{GRAPH_BASE}/{session_id}",
                headers={
                    "Authorization": f"OAuth {access_token}",
                    "file_offset": "0",
                },
                content=file_bytes,
            )
            upload_data = upload_res.json()
    except MediaUploadError:
        raise
    except Exception as exc:
        logger.exception("Resumable upload failed for %s", filename)
        raise MediaUploadError(f"Could not reach Meta to upload the file: {exc}") from exc

    handle = upload_data.get("h")
    if not handle:
        raise MediaUploadError(_meta_message(upload_data, upload_res.status_code))

    logger.info("Resumable upload OK: filename=%s mime=%s", filename, mime)
    return handle


def upload_media(
    account: WhatsAppAccount,
    file_bytes: bytes,
    filename: str,
    mime: str,
) -> str:
    """
    Upload media for sending and return the media id. Meta retains it for 30 days.

    The generalised twin of erpnext_client.upload_to_meta — see the module docstring
    for why that one is not simply replaced by this.
    """
    validate_upload(file_bytes, mime)
    access_token = decrypt_token(account.access_token_encrypted)

    try:
        with httpx.Client(timeout=120) as client:
            res = client.post(
                f"{GRAPH_BASE}/{account.phone_number_id}/media",
                headers={"Authorization": f"Bearer {access_token}"},
                data={"messaging_product": "whatsapp", "type": mime},
                files={"file": (filename, file_bytes, mime)},
            )
        data = res.json()
    except Exception as exc:
        logger.exception("Media upload failed for %s", filename)
        raise MediaUploadError(f"Could not reach Meta to upload the file: {exc}") from exc

    media_id = data.get("id")
    if not media_id:
        raise MediaUploadError(_meta_message(data, res.status_code))
    return media_id


def _meta_message(data: dict, status_code: int) -> str:
    """Pull the most useful line out of a Graph API error body."""
    err = (data or {}).get("error") or {}
    return (
        err.get("error_user_msg")
        or err.get("message")
        or f"Meta returned HTTP {status_code}"
    )
